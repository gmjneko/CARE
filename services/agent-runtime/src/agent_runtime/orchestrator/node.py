"""Generic LangGraph AgentNode wrapping any AgentAdapter."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import get_runtime
from langgraph.types import interrupt

from agent_runtime.adapters.base import (
    AgentSession,
    InteractionResponse,
    ModelEndpoint,
    SessionConfig,
    ThinkingLevel,
)
from agent_runtime.adapters.errors import AdapterError, AdapterProtocolError
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.orchestrator.inflight import InflightNodeStore
from agent_runtime.orchestrator.state import AgentChanges, NodeOutput, WorkflowState
from agent_runtime.protocol.events import (
    AgentEvent,
    InteractionRequestEvent,
    NodeCompletedEvent,
    NodeStartedEvent,
    TurnCompletedEvent,
    WorkflowEvent,
)

InteractionHandler = Callable[[InteractionRequestEvent], Awaitable[InteractionResponse]]
InteractionMode = Literal["cancel", "wait", "suspend"]
PromptBuilder = Callable[[WorkflowState], str]


class _SuspendSignal:
    """内部哨兵：由 runner.suspend() / suspend_all() 注入，通知等待中的热交互立即冷挂起。"""


SUSPEND_SIGNAL = _SuspendSignal()


@dataclass
class PendingInteraction:
    """记录某个活跃 run 当前正阻塞等待人工回复的交互请求及其 Future。"""

    request: InteractionRequestEvent
    future: asyncio.Future[InteractionResponse | _SuspendSignal]


@dataclass(frozen=True)
class StreamPacket:
    """Internal packet written to LangGraph's custom stream writer."""

    node_id: str
    event: AgentEvent | WorkflowEvent


@dataclass(frozen=True)
class AgentNodeConfig:
    """Code-first configuration for a single LangGraph node backed by an AgentAdapter."""

    id: str
    role: str
    prompt_builder: PromptBuilder
    adapter: str = "pi"
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    tools: tuple[str, ...] | None = None
    no_tools: bool = False
    endpoint: ModelEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    extensions: tuple[Path, ...] = ()
    persist_session: bool = False


@dataclass
class RunContext:
    """Shared runtime state injected into compiled LangGraph agent nodes."""

    registry: AdapterRegistry
    active_run_ids: dict[str, str] = field(default_factory=dict)
    active_sessions: dict[str, dict[str, AgentSession]] = field(default_factory=dict)
    interaction_handlers: dict[str, InteractionHandler] = field(default_factory=dict)
    interaction_modes: dict[str, InteractionMode] = field(default_factory=dict)
    pending_interactions: dict[str, dict[str, PendingInteraction]] = field(default_factory=dict)
    suspended_runs: set[str] = field(default_factory=set)
    inflight_store: InflightNodeStore | None = None


class AgentTurnState(WorkflowState):
    continuation: str | None


def build_agent_node(
    node_config: AgentNodeConfig,
    run_context: RunContext,
) -> CompiledStateGraph:
    """Run Pi and await cold decisions in separate checkpointed graph steps."""

    async def run_turn(state: AgentTurnState) -> dict[str, Any]:
        prev_output = state["node_outputs"].get(node_config.id)
        thread_id = state["thread_id"]
        # Command(resume=...) 恢复时 state['run_id'] 可能是挂起前旧值，优先取当前活跃 run_id
        run_id = run_context.active_run_ids.get(thread_id) or state["run_id"]
        adapter = run_context.registry.get(node_config.adapter)

        # 节点开启持久化时，自动复用同一 thread_id 下该节点上次落盘的 session_ref（节点间互不混用）
        prev_session_ref = (
            prev_output.session_ref
            if (node_config.persist_session and prev_output is not None)
            else None
        )

        info = get_runtime().execution_info
        assert info is not None
        task_id = info.task_id
        inflight = run_context.inflight_store
        if inflight is not None:
            for row in await inflight.list_for_thread(thread_id):
                if row.task_id == task_id:
                    # The worker died inside this specific turn. Commit its Pi data
                    # before the separate wait step consumes a durable resume command.
                    output = NodeOutput(
                        node_id=node_config.id,
                        role=node_config.role,
                        status="interrupted",
                        summary="",
                        session_ref=row.session_ref,
                        pending_interaction=row.pending_interaction,
                    )
                    return {"status": "interrupted", "node_outputs": {node_config.id: output}}

        session_config = SessionConfig(
            cwd=Path(state["workspace_dir"]),
            endpoint=node_config.endpoint,
            thinking_level=node_config.thinking_level,
            system_prompt=node_config.system_prompt,
            append_system_prompt=node_config.append_system_prompt,
            tools=node_config.tools,
            no_tools=node_config.no_tools,
            extensions=node_config.extensions,
            persist_session=node_config.persist_session,
            session_ref=prev_session_ref,
        )

        writer = get_stream_writer()
        writer(
            StreamPacket(
                node_id=node_config.id,
                event=NodeStartedEvent(
                    node_id=node_config.id,
                    role=node_config.role,
                    adapter=node_config.adapter,
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
            run_context.active_sessions.setdefault(run_id, {})[node_config.id] = session
            session_ref = session.session_ref or session_ref
            # 握手拿到 session_ref 后立刻写入预写表，防止节点运行中途遭遇 kill -9 丢失会话指针
            if inflight is not None:
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
                        # 阻塞交互需先完成预写与 pending_interactions 登记，再向流发射事件，
                        # 防止调用方在收到事件瞬间调用 respond_interaction() / suspend() 发生竞态。
                        outcome = await _handle_blocking_interaction(
                            run_context=run_context,
                            session=session,
                            writer=writer,
                            run_id=run_id,
                            thread_id=thread_id,
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
                sessions = run_context.active_sessions.get(run_id)
                if sessions is not None and sessions.get(node_config.id) is session:
                    del sessions[node_config.id]
                    if not sessions:
                        run_context.active_sessions.pop(run_id, None)
                pending_map = run_context.pending_interactions.get(run_id)
                if pending_map is not None:
                    pending_map.pop(node_config.id, None)
                    if not pending_map:
                        run_context.pending_interactions.pop(run_id, None)

        suspended = interrupted_interaction is not None or (
            run_id in run_context.suspended_runs
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
        return {
            "node_outputs": {node_config.id: output},
            "latest_output": output.summary
            if output.status == "completed"
            else state["latest_output"],
            "status": "running" if output.status == "completed" else output.status,
            "failure": output.failure,
        }

    def await_resume(state: AgentTurnState) -> dict[str, Any]:
        output = state["node_outputs"][node_config.id]
        reply = interrupt({"node_id": output.node_id, "status": output.status})
        continuation = reply["prompt"]
        if not continuation and output.session_ref:
            continuation = "Continue from where you left off."
        return {"status": "running", "failure": None, "continuation": continuation}

    graph = StateGraph(AgentTurnState, input_schema=WorkflowState, output_schema=AgentChanges)
    graph.add_node("run_turn", run_turn)
    graph.add_node("await_resume", await_resume)

    graph.add_conditional_edges(START, lambda s: "run_turn" if s["status"] == "running" else END)
    graph.add_conditional_edges(
        "run_turn", lambda s: END if s["status"] == "running" else "await_resume"
    )
    graph.add_edge("await_resume", "run_turn")
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
    run_context: RunContext,
    session: AgentSession,
    writer: Callable[[Any], None],
    run_id: str,
    thread_id: str,
    node_id: str,
    role: str,
    task_id: str,
    session_ref: str | None,
    event: InteractionRequestEvent,
) -> Literal["responded", "interrupted"]:
    """处理节点内阻塞式交互请求：支持回调、无头自动取消、即时冷挂起与在线热等待（含进程退出监听）。"""
    inflight = run_context.inflight_store
    if inflight is not None:
        await inflight.upsert(
            thread_id,
            node_id,
            role=role,
            task_id=task_id,
            session_ref=session_ref,
            pending_interaction=event,
        )

    async def clear_pending() -> None:
        if inflight is not None:
            await inflight.upsert(
                thread_id,
                node_id,
                role=role,
                task_id=task_id,
                session_ref=session_ref,
                pending_interaction=None,
            )

    handler = run_context.interaction_handlers.get(run_id)
    if handler is not None:
        writer(StreamPacket(node_id=node_id, event=event))
        response = await handler(event)
        await session.respond_interaction(response)
        await clear_pending()
        return "responded"

    mode = run_context.interaction_modes.get(run_id, "cancel")
    if mode == "cancel":
        writer(StreamPacket(node_id=node_id, event=event))
        # 无头模式立即取消阻塞弹窗，避免子进程永久挂死
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
    run_context.pending_interactions.setdefault(run_id, {})[node_id] = PendingInteraction(
        request=event,
        future=reply_future,
    )
    writer(StreamPacket(node_id=node_id, event=event))

    # 并发监听底层 Pi 子进程退出，避免等待用户输入期间 Pi 崩溃/被杀导致永久死等
    closed_task = asyncio.create_task(session.wait_closed(), name=f"wait-closed-{run_id}-{node_id}")
    try:
        await asyncio.wait(
            {reply_future, closed_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        pending_map = run_context.pending_interactions.get(run_id)
        if pending_map is not None:
            pending_map.pop(node_id, None)
            if not pending_map:
                run_context.pending_interactions.pop(run_id, None)
        if not closed_task.done():
            closed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await closed_task

    # 情况 A：等待期间 Pi 子进程先退出，立即降级为可恢复冷中断
    if not reply_future.done():
        reply_future.cancel()
        return "interrupted"

    decision = reply_future.result()
    # 情况 B：外部触发 runner.suspend() / suspend_all()，优雅取消弹窗并转冷中断
    if isinstance(decision, _SuspendSignal):
        await _gracefully_cancel_for_suspend(session, event.request_id)
        return "interrupted"

    # 情况 C：收到在线热回复，清除预写中的待回复弹窗并原路回传给存活的 Pi 子进程
    try:
        await session.respond_interaction(decision)
    except AdapterError:
        # 回传瞬间若子进程刚好断开，平滑降级为冷中断，保留待回复弹窗供 resume 恢复
        return "interrupted"
    await clear_pending()
    return "responded"


async def _gracefully_cancel_for_suspend(session: AgentSession, request_id: str) -> None:
    """冷挂起前尽力取消悬空弹窗，随后由 open_session 退出阶段优雅关闭 Pi 子进程。"""
    with contextlib.suppress(AdapterError, OSError):
        await session.respond_interaction(
            InteractionResponse(request_id=request_id, decision="cancel")
        )
