"""WorkflowRunner: compiles LangGraph workflows with SQLite checkpoints and event streams."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.adapters.base import InteractionResponse, ModelEndpoint, ThinkingLevel
from agent_runtime.adapters.errors import AdapterError
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.orchestrator.inflight import InflightNodeRecord, InflightNodeStore
from agent_runtime.orchestrator.node import (
    SUSPEND_SIGNAL,
    InteractionHandler,
    InteractionMode,
    RunContext,
    StreamPacket,
)
from agent_runtime.orchestrator.registry import WorkflowBuilder, WorkflowRegistry
from agent_runtime.orchestrator.state import NodeOutput, WorkflowState
from agent_runtime.protocol.events import (
    AgentFailure,
    InteractionRequestEvent,
    RuntimeEvent,
    RuntimeEventPayload,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
    WorkflowResumedEvent,
    WorkflowStartedEvent,
)


class ThreadState(BaseModel):
    """线程综合状态视图：合并 LangGraph Checkpoint 与 SQLite 预写表，供重启后还原 UI 与断点恢复。"""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    status: Literal["completed", "interrupted", "aborted", "failed", "crashed", "running"]
    resumable: bool
    node_outputs: dict[str, NodeOutput] = Field(default_factory=dict)
    next_nodes: tuple[str, ...] = ()
    pending_interaction: InteractionRequestEvent | None = None
    interrupted_node_id: str | None = None
    interrupt_payload: dict[str, Any] = Field(default_factory=dict)
    inflight_nodes: tuple[InflightNodeRecord, ...] = ()


class WorkflowRunner:
    """Production entry point for executing code-defined LangGraph Agent workflows."""

    def __init__(
        self,
        adapter_registry: AdapterRegistry,
        workflow_registry: WorkflowRegistry | None = None,
        *,
        checkpoint_db: str | Path = ":memory:",
    ) -> None:
        self._run_context = RunContext(registry=adapter_registry)
        self._workflow_registry = workflow_registry or WorkflowRegistry()
        self._checkpoint_db = str(checkpoint_db)
        self._exit_stack: AsyncExitStack | None = None
        self._checkpointer: AsyncSqliteSaver | None = None
        self._inflight_store: InflightNodeStore | None = None

    async def __aenter__(self) -> WorkflowRunner:
        # 磁盘 SQLite 路径自动创建父目录（如 ~/.care/）
        if self._checkpoint_db != ":memory:":
            Path(self._checkpoint_db).expanduser().parent.mkdir(parents=True, exist_ok=True)
            conn_str = str(Path(self._checkpoint_db).expanduser())
        else:
            conn_str = ":memory:"

        stack = AsyncExitStack()
        try:
            checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(conn_str)
            )
            await checkpointer.setup()
            # 复用同一个 SQLite 连接与并发锁初始化预写表，零额外连接且无锁竞争
            inflight_store = InflightNodeStore(checkpointer.conn, checkpointer.lock)
            await inflight_store.setup()
        except BaseException:
            await stack.aclose()
            raise
        self._exit_stack = stack
        self._checkpointer = checkpointer
        self._inflight_store = inflight_store
        self._run_context.inflight_store = inflight_store
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._checkpointer = None
        self._inflight_store = None
        self._run_context.inflight_store = None

    async def run(
        self,
        workflow: str | WorkflowBuilder,
        prompt: str,
        workspace_dir: str | Path,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        adapter: str = "pi",
        endpoint: ModelEndpoint | None = None,
        thinking_level: ThinkingLevel | None = None,
        persist_session: bool = True,
        interaction_handler: InteractionHandler | None = None,
        interaction_mode: InteractionMode = "cancel",
    ) -> AsyncIterator[RuntimeEvent]:
        """发起一次新工作流运行，或携带既有 `thread_id` 进行连续多轮对话。"""
        checkpointer = self._require_checkpointer()
        workflow_name, builder = self._resolve_workflow(workflow)
        compiled = _compile_workflow(
            builder,
            self._run_context,
            checkpointer=checkpointer,
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
            persist_session=persist_session,
        )

        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"
        effective_thread_id = thread_id or effective_run_id

        initial_state: WorkflowState = {
            "run_id": effective_run_id,
            "thread_id": effective_thread_id,
            "workflow_id": workflow_name,
            "prompt": prompt,
            "workspace_dir": str(Path(workspace_dir).resolve()),
            # merge_node_outputs 会自动合并保留同 thread_id 历史节点的 session_ref
            "node_outputs": {},
            "latest_output": "",
            "status": "running",
            "failure": None,
        }

        with self._claim_run(effective_thread_id, effective_run_id):
            try:
                existing_state, _, paused = await self._inspect(
                    compiled, effective_thread_id, cleanup=True
                )
            except KeyError:
                existing_state, paused = None, None

            if existing_state is not None and existing_state.resumable:
                if existing_state.status not in ("failed", "aborted"):
                    raise ValueError(
                        f"Thread '{effective_thread_id}' is {existing_state.status}; "
                        "call resume() or discard()"
                    )
                if paused is not None:
                    initial_state["node_outputs"] = {
                        paused.node_id: paused.model_copy(update={"pending_interaction": None})
                    }

            async for event in self._stream_execution(
                compiled=compiled,
                workflow_name=workflow_name,
                run_id=effective_run_id,
                thread_id=effective_thread_id,
                graph_input=initial_state,
                opening_event=WorkflowStartedEvent(preset=workflow_name, prompt=prompt),
                interaction_handler=interaction_handler,
                interaction_mode=interaction_mode,
            ):
                yield event

    async def resume(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
        *,
        run_id: str | None = None,
        resume_value: Any = None,
        interaction_response: InteractionResponse | None = None,
        prompt: str | None = None,
        adapter: str = "pi",
        endpoint: ModelEndpoint | None = None,
        thinking_level: ThinkingLevel | None = None,
        persist_session: bool = True,
        interaction_handler: InteractionHandler | None = None,
        interaction_mode: InteractionMode = "cancel",
    ) -> AsyncIterator[RuntimeEvent]:
        """统一断点恢复入口：自动判定并恢复节点间 interrupt()、冷挂起交互、失败/中止或强杀崩溃。"""
        checkpointer = self._require_checkpointer()
        workflow_name, builder = self._resolve_workflow(workflow)
        compiled = _compile_workflow(
            builder,
            self._run_context,
            checkpointer=checkpointer,
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
            persist_session=persist_session,
        )

        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"
        with self._claim_run(thread_id, effective_run_id):
            state, snapshot, pause = await self._inspect(compiled, thread_id, cleanup=True)
            if not state.resumable:
                raise ValueError(
                    f"Thread '{thread_id}' is already {state.status}; start a new turn with run()"
                )

            graph_input: Any = None
            staged_reply: dict[str, Any] | None = None
            if state.status == "crashed":
                reason: Literal["workflow_interrupt", "agent_interaction", "failure", "crash"] = (
                    "crash"
                )
                text = _format_resume_prompt(
                    pending_interaction=state.pending_interaction,
                    interaction_response=interaction_response,
                    prompt=prompt,
                )
                if state.inflight_nodes:
                    # Native replay materializes the unfinished Pi turn as a checkpointed pause.
                    staged_reply = {"prompt": text}
                elif text is not None:
                    raise ValueError(
                        "Cannot pass a continuation prompt when resuming a crash with no started "
                        "agent turn"
                    )
            elif pause is None:
                reason = "workflow_interrupt"
                decision = (
                    resume_value
                    if resume_value is not None
                    else (interaction_response.model_dump() if interaction_response else prompt)
                )
                if decision is None:
                    raise ValueError("A workflow interrupt requires a resume value")
                graph_input = Command(resume=decision)
            else:
                reason = "agent_interaction" if state.status == "interrupted" else "failure"
                text = _format_resume_prompt(
                    pending_interaction=state.pending_interaction,
                    interaction_response=interaction_response,
                    prompt=prompt,
                )
                graph_input = Command(resume={"prompt": text})

            async for event in self._stream_execution(
                compiled=compiled,
                workflow_name=workflow_name,
                run_id=effective_run_id,
                thread_id=thread_id,
                graph_input=graph_input,
                opening_event=WorkflowResumedEvent(
                    preset=workflow_name, thread_id=thread_id, resumed_from=reason
                ),
                interaction_handler=interaction_handler,
                interaction_mode=interaction_mode,
                staged_reply=staged_reply,
            ):
                yield event

    async def discard(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        """放弃未完成的挂起/崩溃/失败轮次，保留已落盘的 session_ref 并收口为不可恢复终态。"""
        checkpointer = self._require_checkpointer()
        store = self._require_inflight_store()
        _, builder = self._resolve_workflow(workflow)
        compiled = _compile_workflow(builder, self._run_context, checkpointer=checkpointer)

        with self._claim_run(thread_id, f"discard-{uuid4().hex[:12]}"):
            state, _, paused = await self._inspect(compiled, thread_id, cleanup=True)
            if not state.resumable:
                return state

            final_status: Literal["failed", "aborted"] = (
                "failed" if state.status == "failed" else "aborted"
            )
            preserved: dict[str, NodeOutput] = {}
            for row in state.inflight_nodes:
                existing = state.node_outputs.get(row.node_id)
                if existing is not None:
                    preserved[row.node_id] = existing.model_copy(
                        update={"status": "aborted", "pending_interaction": None}
                    )
            if paused is not None:
                preserved[paused.node_id] = paused.model_copy(
                    update={"status": final_status, "pending_interaction": None}
                )

            await _close_resumable_turn(
                checkpointer,
                thread_id=thread_id,
                node_outputs=preserved,
                status=final_status,
                failure=(
                    paused.failure if (paused is not None and final_status == "failed") else None
                ),
            )
            for row in state.inflight_nodes:
                await store.delete(row)

            closed, _, _ = await self._inspect(compiled, thread_id, cleanup=True)
            return closed

    async def respond_interaction(self, run_id: str, response: InteractionResponse) -> None:
        """向正在线热等待的节点回传用户审批结果或提问回答。"""
        pending = self._run_context.pending_interactions.get(run_id)
        if pending is None:
            raise KeyError(f"No pending interaction found for run_id '{run_id}'")
        if pending.request.request_id != response.request_id:
            raise ValueError(
                f"Interaction request_id mismatch for run_id '{run_id}': "
                f"expected '{pending.request.request_id}', got '{response.request_id}'"
            )
        if not pending.future.done():
            pending.future.set_result(response)

    async def suspend(self, run_id: str) -> None:
        """优雅冷挂起指定活跃运行：落盘 session_ref 与待回复交互，发射 WorkflowInterruptedEvent。"""
        self._run_context.suspended_runs.add(run_id)
        pending = self._run_context.pending_interactions.get(run_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(SUSPEND_SIGNAL)
            return

        session = self._run_context.active_sessions.get(run_id)
        if session is not None:
            with contextlib.suppress(AdapterError, OSError):
                await session.abort()

    async def suspend_all(self) -> None:
        """优雅冷挂起当前全部活跃运行（供桌面端 before-quit / SIGTERM 退出前调用）。"""
        active_ids = set(self._run_context.active_sessions) | set(
            self._run_context.pending_interactions
        )
        for run_id in list(active_ids):
            await self.suspend(run_id)

    async def steer(self, run_id: str, message: str) -> None:
        session = self._require_active_session(run_id)
        await session.steer(message)

    async def follow_up(self, run_id: str, message: str) -> None:
        session = self._require_active_session(run_id)
        await session.follow_up(message)

    async def abort(self, run_id: str) -> None:
        # 若当前正卡在热交互等待期，先以 cancel 唤醒弹窗再发 abort，防止死锁
        pending = self._run_context.pending_interactions.get(run_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(
                InteractionResponse(request_id=pending.request.request_id, decision="cancel")
            )
        session = self._require_active_session(run_id)
        await session.abort()

    async def get_checkpoint_state(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> dict[str, Any]:
        checkpointer = self._require_checkpointer()
        _, builder = self._resolve_workflow(workflow)
        compiled = _compile_workflow(builder, self._run_context, checkpointer=checkpointer)
        snapshot = await compiled.aget_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values)

    async def get_thread_state(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        """查询线程综合状态（合并 LangGraph Checkpoint 与 WAL 预写表），供重启后还原弹窗与恢复。"""
        checkpointer = self._require_checkpointer()
        _, builder = self._resolve_workflow(workflow)
        compiled = _compile_workflow(builder, self._run_context, checkpointer=checkpointer)
        view, _, _ = await self._inspect(compiled, thread_id)
        if thread_id in self._run_context.active_run_ids:
            return view.model_copy(update={"status": "running", "resumable": False})
        return view

    async def _inspect(
        self, compiled: Any, thread_id: str, *, cleanup: bool = False
    ) -> tuple[ThreadState, StateSnapshot, NodeOutput | None]:
        store = self._require_inflight_store()
        records = await store.list_for_thread(thread_id)
        snapshot = await compiled.aget_state(
            {"configurable": {"thread_id": thread_id}}, subgraphs=True
        )
        if not snapshot.values and not snapshot.next:
            raise KeyError(f"No checkpoint found for thread_id '{thread_id}'")
        outputs = dict(snapshot.values.get("node_outputs", {}))
        status = snapshot.values.get("status")
        node = None
        request = None
        payload = {}
        live = []
        paused = None
        for task in snapshot.tasks:
            child = task.state
            if not isinstance(child, StateSnapshot):
                continue
            if child.next == ("await_resume",):
                paused = child.values.get("node_outputs", {}).get(task.name)
                if paused is not None:
                    outputs[paused.node_id] = paused
            pending_ids = {step.id for step in child.tasks}
            for row in records:
                if row.node_id != task.name or row.task_id not in pending_ids:
                    continue
                live.append(row)
                node, request = task.name, row.pending_interaction
                existing = outputs.get(node)
                outputs[node] = NodeOutput(
                    node_id=node,
                    role=row.role,
                    status="interrupted",
                    summary="",
                    session_ref=row.session_ref
                    or (existing.session_ref if existing is not None else None),
                    pending_interaction=request,
                )

        if cleanup:
            for row in records:
                if row not in live:
                    await store.delete(row)
        if paused is not None:
            node, status, request = paused.node_id, paused.status, paused.pending_interaction
        elif snapshot.interrupts:
            status = "interrupted"
            node = snapshot.next[0]
            payload = _extract_interrupt_payload(snapshot.interrupts)
        elif snapshot.next:
            status = "crashed"
            node = node or snapshot.next[0]
        elif status == "running":
            status = "completed"
        view = ThreadState(
            thread_id=thread_id,
            status=status,
            resumable=bool(snapshot.next),
            node_outputs=outputs,
            next_nodes=snapshot.next,
            pending_interaction=request,
            interrupted_node_id=node,
            interrupt_payload=payload,
            inflight_nodes=tuple(live),
        )
        return view, snapshot, paused

    @contextlib.contextmanager
    def _claim_run(self, thread_id: str, run_id: str) -> Iterator[None]:
        active = self._run_context.active_run_ids
        if thread_id in active or run_id in active.values():
            raise ValueError(f"Thread '{thread_id}' or run '{run_id}' is already running")
        active[thread_id] = run_id
        try:
            yield
        finally:
            del active[thread_id]

    async def _stream_execution(
        self,
        *,
        compiled: Any,
        workflow_name: str,
        run_id: str,
        thread_id: str,
        graph_input: Any,
        opening_event: WorkflowStartedEvent | WorkflowResumedEvent,
        interaction_handler: InteractionHandler | None,
        interaction_mode: InteractionMode,
        staged_reply: dict | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        seq = 0

        def _wrap(payload: RuntimeEventPayload, *, node_id: str | None = None) -> RuntimeEvent:
            nonlocal seq
            seq += 1
            return RuntimeEvent(
                seq=seq,
                run_id=run_id,
                workflow_id=workflow_name,
                node_id=node_id,
                payload=payload,
            )

        if interaction_handler is not None:
            self._run_context.interaction_handlers[run_id] = interaction_handler
        self._run_context.interaction_modes[run_id] = interaction_mode

        try:
            yield _wrap(opening_event)
            while True:
                async for chunk in compiled.astream(
                    graph_input,
                    config={"configurable": {"thread_id": thread_id}},
                    stream_mode="custom",
                    subgraphs=True,
                    version="v2",
                    durability="sync",
                ):
                    packet = chunk["data"]
                    if isinstance(packet, StreamPacket):
                        yield _wrap(packet.event, node_id=packet.node_id)
                state, snapshot, pause = await self._inspect(compiled, thread_id, cleanup=True)
                if staged_reply is None:
                    break
                if not snapshot.interrupts or pause is None:
                    raise RuntimeError("Expected the recovered Pi turn to reach its resume gate")
                graph_input = Command(resume=staged_reply)
                staged_reply = None

            node = state.interrupted_node_id
            if state.status == "interrupted":
                reason = (
                    "workflow_interrupt"
                    if pause is None
                    else ("agent_interaction" if state.pending_interaction else "suspended")
                )
                yield _wrap(
                    WorkflowInterruptedEvent(
                        reason=reason,
                        node_id=node,
                        payload=state.interrupt_payload,
                        interaction=state.pending_interaction,
                    ),
                    node_id=node,
                )
            elif state.status in ("failed", "aborted", "completed"):
                yield _wrap(
                    WorkflowCompletedEvent(
                        status=state.status,
                        final_output=snapshot.values.get("latest_output", ""),
                        failure=pause.failure if pause else snapshot.values.get("failure"),
                    )
                )
            else:
                raise RuntimeError(f"Graph stopped with pending nodes {state.next_nodes}")
        finally:
            self._run_context.interaction_handlers.pop(run_id, None)
            self._run_context.interaction_modes.pop(run_id, None)
            self._run_context.pending_interactions.pop(run_id, None)
            self._run_context.suspended_runs.discard(run_id)

    def _resolve_workflow(self, workflow: str | WorkflowBuilder) -> tuple[str, WorkflowBuilder]:
        if isinstance(workflow, str):
            definition = self._workflow_registry.get(workflow)
            return definition.name, definition.build
        name = getattr(workflow, "__name__", "custom_workflow")
        return name, workflow

    def _require_checkpointer(self) -> AsyncSqliteSaver:
        if self._checkpointer is None:
            raise RuntimeError(
                "WorkflowRunner must be used inside 'async with WorkflowRunner(...)'"
            )
        return self._checkpointer

    def _require_inflight_store(self) -> InflightNodeStore:
        if self._inflight_store is None:
            raise RuntimeError(
                "WorkflowRunner must be used inside 'async with WorkflowRunner(...)'"
            )
        return self._inflight_store

    def _require_active_session(self, run_id: str):
        try:
            return self._run_context.active_sessions[run_id]
        except KeyError as exc:
            raise KeyError(f"No active agent session found for run_id '{run_id}'") from exc


def _compile_workflow(
    builder: WorkflowBuilder,
    run_context: RunContext,
    *,
    checkpointer: AsyncSqliteSaver,
    adapter: str = "pi",
    endpoint: ModelEndpoint | None = None,
    thinking_level: ThinkingLevel | None = None,
    persist_session: bool = True,
) -> Any:
    graph = builder(
        run_context,
        adapter=adapter,
        endpoint=endpoint,
        thinking_level=thinking_level,
        persist_session=persist_session,
    )
    return graph.compile(checkpointer=checkpointer)


async def _close_resumable_turn(
    checkpointer: AsyncSqliteSaver,
    *,
    thread_id: str,
    node_outputs: dict[str, NodeOutput],
    status: Literal["failed", "aborted"],
    failure: AgentFailure | None,
) -> None:
    """Close out a paused or crashed LangGraph cursor without running workflow nodes."""
    graph: StateGraph[Any] = StateGraph(WorkflowState)
    graph.add_node("_discard", lambda _: {})
    graph.add_edge(START, "_discard")
    graph.add_edge("_discard", END)
    await graph.compile(checkpointer=checkpointer).ainvoke(
        {
            "node_outputs": node_outputs,
            "status": status,
            "failure": failure,
        },
        config={"configurable": {"thread_id": thread_id}},
        durability="sync",
    )


def _extract_interrupt_payload(raw_interrupts: Any) -> dict[str, Any]:
    """从 LangGraph `__interrupt__` 元组中归一化提取字典载荷。"""
    if isinstance(raw_interrupts, (list, tuple)) and raw_interrupts:
        first = raw_interrupts[0]
        value = getattr(first, "value", first)
        if isinstance(value, dict):
            return dict(value)
        return {"value": value}
    if isinstance(raw_interrupts, dict):
        return dict(raw_interrupts)
    return {"value": raw_interrupts}


def _format_resume_prompt(
    *,
    pending_interaction: InteractionRequestEvent | None,
    interaction_response: InteractionResponse | None,
    prompt: str | None,
) -> str | None:
    """将冷恢复时的用户审批裁决或问题回答格式化为续接指令（同时支持直接传入补充 prompt）。"""
    if pending_interaction is not None and interaction_response is None:
        raise ValueError(
            f"A response for request_id '{pending_interaction.request_id}' is required"
        )
    if interaction_response is not None and (
        pending_interaction is None
        or interaction_response.request_id != pending_interaction.request_id
    ):
        raise ValueError("Interaction request_id does not match the pending request")
    parts: list[str] = []
    if interaction_response is not None:
        assert pending_interaction is not None
        title = pending_interaction.title or "request"
        if pending_interaction.kind == "user_input":
            if interaction_response.decision != "approve":
                parts.append(f"The user cancelled the question ({title}).")
            else:
                if interaction_response.value is None:
                    raise ValueError("Answering a pending question requires a value")
                answer = interaction_response.value
                parts.append(f"The user answered your question ({title}): {answer}")
        else:
            detail = f"decision={interaction_response.decision}"
            if interaction_response.feedback:
                detail = f"{detail}, feedback={interaction_response.feedback}"
            parts.append(f"The user responded to your approval request ({title}): {detail}.")
        parts.append("Continue your task accordingly.")

    if prompt and prompt.strip():
        parts.append(prompt.strip())

    return "\n".join(parts) if parts else None
