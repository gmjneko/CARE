"""Generic LangGraph AgentNode and ApprovalNode factories."""

from __future__ import annotations

import asyncio
import contextlib
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
    TurnCompletedEvent,
    WorkflowEvent,
)

PromptBuilder = Callable[[WorkflowState], str]

# Explicit internal contract constants shared with recovery.py (single source of truth).
AGENT_RUN_NODE = "run_turn"
AGENT_RESUME_NODE = "await_resume"
AGENT_RESUME_PROMPT_KEY = "prompt"
APPROVAL_INTERACTION_KEY = "interaction"
APPROVAL_CONTENT_KEY = "content"


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
    tool_policy: ToolPolicy = "full"
    tools: tuple[str, ...] | None = None
    endpoint: ModelEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool | None = None


class AgentTurnState(WorkflowState):
    """Subgraph-private state isolating turn loop status from parent WorkflowState.status."""

    turn_status: Literal["running", "completed", "interrupted", "failed", "aborted"]
    continuation: str | None


def build_agent_node(node_config: AgentNodeConfig) -> CompiledStateGraph:
    """Build a pure topology AgentNode subgraph whose runtime dependencies arrive via context."""

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

        override = ctx.options.node_overrides.get(node_config.id)
        effective_adapter_kind = (
            override.adapter
            if override is not None and override.adapter is not None
            else (node_config.adapter if node_config.adapter is not None else ctx.options.adapter)
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
        effective_tool_policy = (
            override.tool_policy
            if override is not None and override.tool_policy is not None
            else node_config.tool_policy
        )
        effective_tools = (
            override.tools
            if override is not None and override.tools is not None
            else node_config.tools
        )

        adapter = ctx.adapters.get(effective_adapter_kind)
        prev_session_ref = state["node_sessions"].get(node_config.id) if effective_persist else None

        if prev_session_ref is not None and not adapter.capabilities.session_resume:
            raise AdapterCapabilityError(
                f"Adapter '{effective_adapter_kind}' does not support resuming persisted sessions"
            )

        inflight = ctx.inflight_store
        for row in await inflight.list_for_thread(thread_id):
            if row.task_id == task_id:
                # The worker died inside this specific turn. Commit its durable session data
                # before the separate wait step consumes a durable resume command.
                output = NodeOutput(
                    node_id=node_config.id,
                    role=node_config.role,
                    status="interrupted",
                    summary="",
                    session_ref=row.session_ref,
                    pending_interaction=row.pending_interaction,
                )
                recovered: dict[str, Any] = {
                    "turn_status": "interrupted",
                    "node_outputs": {node_config.id: output},
                }
                if row.session_ref:
                    recovered["node_sessions"] = {node_config.id: row.session_ref}
                return recovered

        session_config = SessionConfig(
            cwd=Path(state["workspace_dir"]),
            endpoint=effective_endpoint,
            thinking_level=effective_thinking,
            system_prompt=node_config.system_prompt,
            append_system_prompt=node_config.append_system_prompt,
            tool_policy=effective_tool_policy,
            tools=effective_tools,
            persist_session=effective_persist,
            session_ref=prev_session_ref,
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

                    if isinstance(event, TurnCompletedEvent):
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
            status=terminal.status if terminal and not suspended else "interrupted",
            summary=terminal.final_text if terminal else "",
            session_ref=session_ref,
            usage=terminal.usage if terminal else None,
            failure=terminal.failure if terminal and not suspended else None,
            pending_interaction=interrupted_interaction,
        )
        _emit_node_completed(writer, output)

        update: dict[str, Any] = {
            "node_outputs": {node_config.id: output},
            "latest_output": output.summary
            if output.status == "completed"
            else state["latest_output"],
            "turn_status": output.status,
        }
        if session_ref:
            update["node_sessions"] = {node_config.id: session_ref}
        return update

    def await_resume(state: AgentTurnState) -> dict[str, Any]:
        output = state["node_outputs"][node_config.id]
        reply = interrupt({"node_id": output.node_id, "status": output.status})
        continuation = reply[AGENT_RESUME_PROMPT_KEY]
        if not continuation and state["node_sessions"].get(node_config.id):
            continuation = "Continue from where you left off."
        return {"turn_status": "running", "continuation": continuation}

    graph = StateGraph(AgentTurnState, input_schema=WorkflowState, output_schema=AgentChanges)
    graph.add_node(AGENT_RUN_NODE, run_turn)
    graph.add_node(AGENT_RESUME_NODE, await_resume)

    graph.add_conditional_edges(
        START, lambda s: AGENT_RUN_NODE if s["status"] == "running" else END
    )
    graph.add_conditional_edges(
        AGENT_RUN_NODE, lambda s: END if s["turn_status"] == "completed" else AGENT_RESUME_NODE
    )
    graph.add_edge(AGENT_RESUME_NODE, AGENT_RUN_NODE)
    return graph.compile(name=node_config.id)


def continue_if_running(next_node: str) -> Callable[[WorkflowState], str]:
    """Conditional edge router for non-agent custom nodes (e.g. approval gates).

    Halts downstream nodes when a custom node returns a non-``"running"`` status.
    ``AgentNode`` subgraphs pause inside ``await_resume`` on failure/abort/interruption
    rather than exiting to downstream edges.
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
        raise ValueError(
            f"Approval node '{node_id}' requires a mapping with "
            "'decision' ('approve' | 'deny' | 'cancel')"
        )
    decision = raw.get("decision")
    if decision not in ("approve", "deny", "cancel"):
        raise ValueError(
            f"Approval node '{node_id}' received invalid decision {decision!r}; "
            "expected 'approve', 'deny', or 'cancel'"
        )
    feedback = raw.get("feedback")
    if feedback is not None and not isinstance(feedback, str):
        raise ValueError(f"Approval node '{node_id}' field 'feedback' must be a string or None")
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
