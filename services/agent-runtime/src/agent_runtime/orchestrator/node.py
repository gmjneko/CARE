"""Generic LangGraph AgentNode and ApprovalNode factories."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import get_runtime
from langgraph.types import interrupt

from agent_runtime.adapters.base import (
    AgentSession,
    ModelEndpoint,
    SessionConfig,
    ThinkingLevel,
    ToolPolicy,
)
from agent_runtime.adapters.errors import (
    AdapterCapabilityError,
    AdapterError,
    AdapterProtocolError,
)
from agent_runtime.errors import InvalidInputError
from agent_runtime.orchestrator.live_runs import (
    ExecutionContext,
    PendingInteraction,
    _SuspendSignal,
)
from agent_runtime.orchestrator.state import AgentChanges, NodeOutput, WorkflowState
from agent_runtime.protocol.events import (
    AgentEvent,
    InteractionRequestEvent,
    InteractionResponse,
    NodeCompletedEvent,
    NodeStartedEvent,
    ToolCompletedEvent,
    TurnCompletedEvent,
    WorkflowEvent,
)

PromptBuilder = Callable[[WorkflowState], str]
TurnCompletionCheck = Callable[[NodeOutput, tuple[ToolCompletedEvent, ...]], bool]

# Explicit internal contract constants shared with recovery.py (single source of truth).
AGENT_RUN_NODE = "run_turn"
AGENT_WAIT_NODE = "await_input"
AGENT_RESUME_ACTION_KEY = "action"
AGENT_RESUME_PROMPT_KEY = "prompt"
APPROVAL_INTERACTION_KEY = "interaction"
APPROVAL_CONTENT_KEY = "content"


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamPacket:
    """Internal packet written to LangGraph's custom stream writer."""

    node_id: str
    event: AgentEvent | WorkflowEvent


@dataclass(frozen=True)
class AgentNodeConfig:
    """Declarative configuration for a single LangGraph node backed by an AgentAdapter."""

    id: str
    role: str
    prompt_builder: PromptBuilder
    adapter: str | None = None
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    # 默认 None 而非 "full"，用于区分“未显式配置（可从上游继承）”与“显式覆盖为 full”
    tool_policy: ToolPolicy | None = None
    tools: tuple[str, ...] | None = None
    endpoint: ModelEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool | None = None
    # 指定本轮上游节点 ID：首次运行时从其会话 fork 独立副本，并默认继承其提示词与工具配置
    inherit_session_from: str | None = None
    # 宽容模式：为 True 时，抑制“继承会话的同时显式覆盖提示词/工具”产生的警告日志
    inherit_session_lenient: bool = False
    should_complete: TurnCompletionCheck | None = None


class AgentTurnState(WorkflowState):
    """Subgraph-private state isolating turn loop status from parent WorkflowState.status."""

    turn_status: Literal["running", "completed", "waiting", "interrupted", "failed", "aborted"]
    continuation: str | None


def build_agent_node(node_config: AgentNodeConfig) -> CompiledStateGraph:
    """Build a pure topology AgentNode subgraph whose runtime dependencies arrive via context."""
    inherit_source: str | None = None
    if node_config.inherit_session_from is not None:
        inherit_source = node_config.inherit_session_from.strip()
        if not inherit_source:
            raise ValueError("inherit_session_from must be non-empty when provided")
        if inherit_source == node_config.id:
            raise ValueError(f"Agent node '{node_config.id}' cannot inherit session from itself")
    elif node_config.inherit_session_lenient:
        raise ValueError("inherit_session_lenient=True requires inherit_session_from to be set")

    async def run_turn(state: AgentTurnState) -> dict[str, Any]:
        runtime = get_runtime(ExecutionContext)
        ctx = runtime.context
        if ctx is None:
            raise RuntimeError(
                f"Agent node '{node_config.id}' requires ExecutionContext via LangGraph context="
            )
        info = runtime.execution_info
        assert info is not None
        task_id = info.task_id

        thread_id = ctx.thread_id
        run_id = ctx.run_id

        source_output = (
            state["node_outputs"].get(inherit_source) if inherit_source is not None else None
        )
        source_settings = source_output.session_settings if source_output is not None else None

        override = ctx.options.node_overrides.get(node_config.id)
        effective_adapter_kind = (
            override.adapter
            if override is not None and override.adapter is not None
            else (
                node_config.adapter
                if node_config.adapter is not None
                else (
                    source_output.adapter
                    if source_output is not None and source_output.adapter is not None
                    else ctx.options.adapter
                )
            )
        )
        effective_endpoint = (
            override.endpoint
            if override is not None and override.endpoint is not None
            else (
                node_config.endpoint if node_config.endpoint is not None else ctx.options.endpoint
            )
        )
        effective_thinking = (
            override.thinking_level
            if override is not None and override.thinking_level is not None
            else (
                node_config.thinking_level
                if node_config.thinking_level is not None
                else ctx.options.thinking_level
            )
        )
        effective_persist = (
            override.persist_session
            if override is not None and override.persist_session is not None
            else (
                node_config.persist_session
                if node_config.persist_session is not None
                else ctx.options.persist_session
            )
        )

        # 仅记录本节点显式设置的提示词与工具字段；继承会话时按需告警而不计算内容指纹
        overridden_fields: list[str] = []
        if node_config.system_prompt is not None:
            effective_system_prompt: str | None = node_config.system_prompt
            overridden_fields.append("system_prompt")
        elif source_settings is not None:
            effective_system_prompt = source_settings.get("system_prompt")
        else:
            effective_system_prompt = None

        if node_config.append_system_prompt is not None:
            effective_append_system_prompt: str | None = node_config.append_system_prompt
            overridden_fields.append("append_system_prompt")
        elif source_settings is not None:
            effective_append_system_prompt = source_settings.get("append_system_prompt")
        else:
            effective_append_system_prompt = None

        explicit_tool_policy = (
            override.tool_policy
            if override is not None and override.tool_policy is not None
            else node_config.tool_policy
        )
        explicit_tools = (
            override.tools
            if override is not None and override.tools is not None
            else node_config.tools
        )
        if explicit_tool_policy is not None:
            overridden_fields.append("tool_policy")
        if explicit_tools is not None:
            overridden_fields.append("tools")

        # tool_policy 与 tools 互斥，只要本节点显式设置了其中任一项就整体脱离上游工具继承
        if explicit_tool_policy is not None or explicit_tools is not None:
            effective_tool_policy: ToolPolicy = (
                explicit_tool_policy if explicit_tool_policy is not None else "full"
            )
            effective_tools: tuple[str, ...] | None = explicit_tools
        elif source_settings is not None:
            effective_tool_policy = source_settings.get("tool_policy", "full")
            raw_tools = source_settings.get("tools")
            effective_tools = tuple(raw_tools) if raw_tools is not None else None
        else:
            effective_tool_policy = "full"
            effective_tools = None

        adapter = ctx.adapters.get(effective_adapter_kind)
        session_settings: dict[str, Any] = {
            "tool_policy": effective_tool_policy,
            "tools": list(effective_tools) if effective_tools is not None else None,
            "system_prompt": effective_system_prompt,
            "append_system_prompt": effective_append_system_prompt,
        }

        # 先处理崩溃 WAL 补交，确保已 fork 出自身 session_ref 后崩溃的节点不会再次重复 fork
        inflight = ctx.inflight_store
        for row in await inflight.list_for_thread(thread_id):
            if row.task_id == task_id:
                # The worker died inside this specific turn. Commit its durable session data
                # before the separate wait step consumes a durable resume command.
                output = NodeOutput(
                    node_id=node_config.id,
                    role=node_config.role,
                    adapter=effective_adapter_kind,
                    status="interrupted",
                    summary="",
                    session_ref=row.session_ref,
                    session_settings=session_settings,
                    pending_interaction=row.pending_interaction,
                )
                recovered: dict[str, Any] = {
                    "turn_status": "interrupted",
                    "node_outputs": {node_config.id: output},
                }
                if row.session_ref:
                    recovered["node_sessions"] = {node_config.id: row.session_ref}
                return recovered

        # node_outputs 每轮 run() 清空、轮内保留，据此区分“本轮首次进入(fork)”与“轮内续跑(恢复自身)”
        already_started_this_turn = node_config.id in state["node_outputs"]
        prev_session_ref: str | None = None
        fork_from_session_ref: str | None = None

        if inherit_source is not None and not already_started_this_turn:
            if not effective_persist:
                raise ValueError(
                    f"Agent node '{node_config.id}' sets inherit_session_from='{inherit_source}' "
                    "which requires persist_session=True"
                )
            if not adapter.capabilities.session_fork:
                raise AdapterCapabilityError(
                    f"Adapter '{effective_adapter_kind}' does not support "
                    "forking persisted sessions"
                )
            if source_output is None or not source_output.session_ref:
                raise ValueError(
                    f"Agent node '{node_config.id}' cannot inherit session from "
                    f"'{inherit_source}': source node has not produced a persisted "
                    "session in this turn"
                )
            if source_output.adapter != effective_adapter_kind:
                raise AdapterCapabilityError(
                    f"Agent node '{node_config.id}' (adapter='{effective_adapter_kind}') cannot "
                    f"inherit session from '{inherit_source}' (adapter='{source_output.adapter}')"
                )
            if overridden_fields and not node_config.inherit_session_lenient:
                logger.warning(
                    "Agent node '%s' inherits session from '%s' while explicitly setting %s; "
                    "overriding system prompt or tools on an inherited session may invalidate "
                    "prompt cache or conflict with historical tool calls. "
                    "Set inherit_session_lenient=True to silence this warning.",
                    node_config.id,
                    inherit_source,
                    ", ".join(overridden_fields),
                )
            fork_from_session_ref = source_output.session_ref
        elif effective_persist:
            prev_session_ref = state["node_sessions"].get(node_config.id)
            if prev_session_ref is None and already_started_this_turn:
                prev_session_ref = state["node_outputs"][node_config.id].session_ref
            if prev_session_ref is not None and not adapter.capabilities.session_resume:
                raise AdapterCapabilityError(
                    f"Adapter '{effective_adapter_kind}' does not support "
                    "resuming persisted sessions"
                )

        session_config = SessionConfig(
            cwd=Path(state["workspace_dir"]),
            endpoint=effective_endpoint,
            thinking_level=effective_thinking,
            system_prompt=effective_system_prompt,
            append_system_prompt=effective_append_system_prompt,
            tool_policy=effective_tool_policy,
            tools=effective_tools,
            persist_session=effective_persist,
            session_ref=prev_session_ref,
            fork_from_session_ref=fork_from_session_ref,
        )

        writer = get_stream_writer()
        writer(
            StreamPacket(
                node_id=node_config.id,
                event=NodeStartedEvent(
                    node_id=node_config.id,
                    role=node_config.role,
                    adapter=effective_adapter_kind,
                ),
            )
        )

        continuation = state.get("continuation")
        if continuation and prev_session_ref:
            prompt = continuation
        else:
            prompt = node_config.prompt_builder(state)
            if continuation:
                prompt = f"{prompt}\n\n{continuation}"

        terminal: TurnCompletedEvent | None = None
        completed_tools: list[ToolCompletedEvent] = []
        session_ref: str | None = prev_session_ref
        interrupted_interaction: InteractionRequestEvent | None = None

        async with adapter.open_session(session_config) as session:
            ctx.live_runs.register_session(run_id, node_config.id, session, adapter.capabilities)
            session_ref = session.session_ref or session_ref
            await inflight.upsert(
                thread_id,
                node_config.id,
                role=node_config.role,
                task_id=task_id,
                session_ref=session_ref,
                pending_interaction=None,
            )

            try:
                async for event in session.run_turn(prompt):
                    session_ref = session.session_ref or session_ref

                    if isinstance(event, InteractionRequestEvent) and event.blocking:
                        if not adapter.capabilities.interactions:
                            raise AdapterCapabilityError(
                                f"Adapter '{effective_adapter_kind}' emitted a blocking "
                                "interaction but declares capabilities.interactions=False"
                            )
                        outcome = await _handle_blocking_interaction(
                            ctx=ctx,
                            session=session,
                            writer=writer,
                            node_id=node_config.id,
                            role=node_config.role,
                            task_id=task_id,
                            session_ref=session_ref,
                            event=event,
                        )
                        if outcome == "interrupted":
                            interrupted_interaction = event
                            break
                        continue

                    writer(StreamPacket(node_id=node_config.id, event=event))

                    if isinstance(event, ToolCompletedEvent):
                        completed_tools.append(event)
                    elif isinstance(event, TurnCompletedEvent):
                        terminal = event

                session_ref = session.session_ref or session_ref
            finally:
                ctx.live_runs.unregister_session(run_id, node_config.id, session)

        suspended = interrupted_interaction is not None or (
            ctx.live_runs.is_suspended(run_id)
            and (terminal is None or terminal.status == "aborted")
        )
        if terminal is None and not suspended:
            raise AdapterProtocolError(
                f"Agent node '{node_config.id}' ended without TurnCompletedEvent"
            )
        output = NodeOutput(
            node_id=node_config.id,
            role=node_config.role,
            adapter=effective_adapter_kind,
            status=terminal.status if terminal and not suspended else "interrupted",
            summary=terminal.final_text if terminal else "",
            session_ref=session_ref,
            session_settings=session_settings,
            usage=terminal.usage if terminal else None,
            failure=terminal.failure if terminal and not suspended else None,
            pending_interaction=interrupted_interaction,
        )
        if (
            output.status == "completed"
            and node_config.should_complete is not None
            and not node_config.should_complete(output, tuple(completed_tools))
        ):
            output = output.model_copy(update={"status": "waiting"})
        else:
            _emit_node_completed(writer, output)

        update: dict[str, Any] = {
            "node_outputs": {node_config.id: output},
            "latest_output": output.summary
            if output.status == "completed"
            else state["latest_output"],
            "turn_status": output.status,
            "continuation": None,
        }
        if session_ref:
            update["node_sessions"] = {node_config.id: session_ref}
        return update

    def await_input(state: AgentTurnState) -> dict[str, Any]:
        output = state["node_outputs"][node_config.id]
        reply = interrupt({"node_id": output.node_id, "status": output.status})
        if reply.get(AGENT_RESUME_ACTION_KEY) == "complete":
            completed = output.model_copy(update={"status": "completed"})
            _emit_node_completed(get_stream_writer(), completed)
            return {
                "node_outputs": {node_config.id: completed},
                "latest_output": completed.summary,
                "turn_status": "completed",
                "continuation": None,
            }
        continuation = reply[AGENT_RESUME_PROMPT_KEY]
        if not continuation and state["node_sessions"].get(node_config.id):
            continuation = "Continue from where you left off."
        return {
            "turn_status": "running",
            "continuation": continuation
        }

    graph = StateGraph(AgentTurnState, input_schema=WorkflowState, output_schema=AgentChanges)
    graph.add_node(AGENT_RUN_NODE, run_turn)
    graph.add_node(AGENT_WAIT_NODE, await_input)

    graph.add_conditional_edges(
        START, lambda s: AGENT_RUN_NODE if s["status"] == "running" else END
    )
    graph.add_conditional_edges(
        AGENT_RUN_NODE, lambda s: END if s["turn_status"] == "completed" else AGENT_WAIT_NODE
    )
    graph.add_conditional_edges(
        AGENT_WAIT_NODE, lambda s: END if s["turn_status"] == "completed" else AGENT_RUN_NODE
    )
    return graph.compile(name=node_config.id)


def continue_if_running(next_node: str) -> Callable[[WorkflowState], str]:
    """Conditional edge router for non-agent custom nodes (e.g. approval gates).

    Halts downstream nodes when a custom node returns a non-``"running"`` status.
    ``AgentNode`` subgraphs pause inside ``await_input`` when waiting, interrupted,
    failed, or aborted rather than exiting to downstream edges.
    """

    def _route(state: WorkflowState) -> str:
        return next_node if state["status"] == "running" else END

    return _route


_APPROVAL_DECISION_LABELS: dict[Literal["approve", "deny", "cancel"], str] = {
    "approve": "approved",
    "deny": "denied",
    "cancel": "cancelled",
}


def build_approval_node(
    node_id: str,
    *,
    message: str,
    title: str | None = None,
    content_builder: PromptBuilder | None = None,
) -> Callable[[WorkflowState], dict[str, Any]]:
    """Build a workflow-level human approval gate node backed by LangGraph ``interrupt()``.

    Pauses with a structured approval payload (``kind="approval"``, ``message``, optional
    ``title`` and ``content``) and validates the resumed decision strictly against
    ``{"decision": "approve" | "deny" | "cancel", "feedback": str | None}`` (which also
    matches ``InteractionResponse.model_dump()``).
    """
    clean_id = node_id.strip()
    if not clean_id:
        raise ValueError("node_id must be non-empty")
    clean_message = message.strip()
    if not clean_message:
        raise ValueError("message must be non-empty")
    clean_title: str | None = None
    if title is not None:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("title must be non-empty when provided")

    def approval_gate(state: WorkflowState) -> dict[str, Any]:
        if state["status"] != "running":
            return {}

        request = InteractionRequestEvent(
            request_id=f"{state['run_id']}:{clean_id}",
            kind="approval",
            blocking=True,
            title=clean_title,
            message=clean_message,
        )
        interrupt_payload: dict[str, Any] = {APPROVAL_INTERACTION_KEY: request}
        content = (
            content_builder(state) if content_builder is not None else state["latest_output"]
        ).strip()
        if content:
            interrupt_payload[APPROVAL_CONTENT_KEY] = content

        writer = get_stream_writer()
        writer(
            StreamPacket(
                node_id=clean_id,
                event=NodeStartedEvent(node_id=clean_id, role="approval", adapter=None),
            )
        )
        decision, feedback = parse_approval_decision(interrupt(interrupt_payload), node_id=clean_id)
        label = _APPROVAL_DECISION_LABELS[decision]
        summary = f"{label}: {feedback}" if feedback else label
        approved = decision == "approve"
        output = NodeOutput(
            node_id=clean_id,
            role="approval",
            status="completed" if approved else "aborted",
            summary=summary,
        )
        _emit_node_completed(writer, output)
        update: dict[str, Any] = {"node_outputs": {clean_id: output}}
        if not approved:
            update["status"] = "aborted"
        return update

    approval_gate.__name__ = clean_id
    return approval_gate


def parse_approval_decision(
    raw: Any,
    *,
    node_id: str,
) -> tuple[Literal["approve", "deny", "cancel"], str | None]:
    """Validate and normalize an approval decision payload."""
    if not isinstance(raw, Mapping):
        raise InvalidInputError(
            f"Approval node '{node_id}' requires a mapping with "
            "'decision' ('approve' | 'deny' | 'cancel')"
        )
    decision = raw.get("decision")
    if decision not in ("approve", "deny", "cancel"):
        raise InvalidInputError(
            f"Approval node '{node_id}' received invalid decision {decision!r}; "
            "expected 'approve', 'deny', or 'cancel'"
        )
    feedback = raw.get("feedback")
    if feedback is not None and not isinstance(feedback, str):
        raise InvalidInputError(
            f"Approval node '{node_id}' field 'feedback' must be a string or None"
        )
    clean_feedback = feedback.strip() if feedback and feedback.strip() else None
    return decision, clean_feedback


def _emit_node_completed(writer: Callable[[Any], None], output: NodeOutput) -> None:
    writer(
        StreamPacket(
            node_id=output.node_id,
            event=NodeCompletedEvent(
                node_id=output.node_id,
                role=output.role,
                status=output.status,
                summary=output.summary,
                usage=output.usage,
                failure=output.failure,
            ),
        )
    )


async def _handle_blocking_interaction(
    *,
    ctx: ExecutionContext,
    session: AgentSession,
    writer: Callable[[Any], None],
    node_id: str,
    role: str,
    task_id: str,
    session_ref: str | None,
    event: InteractionRequestEvent,
) -> Literal["responded", "interrupted"]:
    """处理节点内阻塞式交互请求：支持回调、无头自动取消、即时冷挂起与在线热等待（含进程退出监听）。"""
    inflight = ctx.inflight_store
    await inflight.upsert(
        ctx.thread_id,
        node_id,
        role=role,
        task_id=task_id,
        session_ref=session_ref,
        pending_interaction=event,
    )

    async def clear_pending() -> None:
        await inflight.upsert(
            ctx.thread_id,
            node_id,
            role=role,
            task_id=task_id,
            session_ref=session_ref,
            pending_interaction=None,
        )

    handler = ctx.live_runs.get_interaction_handler(ctx.run_id)
    if handler is not None:
        writer(StreamPacket(node_id=node_id, event=event))
        response = await handler(event)
        await session.respond_interaction(response)
        await clear_pending()
        return "responded"

    mode = ctx.live_runs.get_interaction_mode(ctx.run_id)
    if mode == "cancel":
        writer(StreamPacket(node_id=node_id, event=event))
        await session.respond_interaction(
            InteractionResponse(request_id=event.request_id, decision="cancel")
        )
        await clear_pending()
        return "responded"

    if mode == "suspend":
        writer(StreamPacket(node_id=node_id, event=event))
        await _gracefully_cancel_for_suspend(session, event.request_id)
        return "interrupted"

    loop = asyncio.get_running_loop()
    reply_future: asyncio.Future[InteractionResponse | _SuspendSignal] = loop.create_future()
    pending = PendingInteraction(request=event, future=reply_future)
    ctx.live_runs.register_pending(ctx.run_id, node_id, pending)
    writer(StreamPacket(node_id=node_id, event=event))

    closed_task = asyncio.create_task(
        session.wait_closed(), name=f"wait-closed-{ctx.run_id}-{node_id}"
    )
    try:
        try:
            await asyncio.wait(
                {reply_future, closed_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            ctx.live_runs.unregister_pending(ctx.run_id, node_id)
            if not closed_task.done():
                closed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await closed_task

        if not reply_future.done():
            reply_future.cancel()
            return "interrupted"

        decision = reply_future.result()
        if isinstance(decision, _SuspendSignal):
            await _gracefully_cancel_for_suspend(session, event.request_id)
            return "interrupted"

        try:
            await session.respond_interaction(decision)
        except AdapterError:
            return "interrupted"
        await clear_pending()
        return "responded"
    finally:
        # Notify any concurrent abort() waiter that the interaction response has been dispatched.
        pending.settled.set()


async def _gracefully_cancel_for_suspend(session: AgentSession, request_id: str) -> None:
    """冷挂起前尽力取消悬空弹窗，随后由 open_session 退出阶段优雅关闭子进程。"""
    with contextlib.suppress(AdapterError, OSError):
        await session.respond_interaction(
            InteractionResponse(request_id=request_id, decision="cancel")
        )
