"""WorkflowRunner: compiles LangGraph workflows with SQLite checkpoints and event streams."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.adapters.base import (
    AgentSession,
    InteractionResponse,
    ModelEndpoint,
    ThinkingLevel,
)
from agent_runtime.adapters.errors import AdapterError
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.orchestrator.inflight import InflightNodeRecord, InflightNodeStore
from agent_runtime.orchestrator.node import (
    SUSPEND_SIGNAL,
    InteractionHandler,
    InteractionMode,
    RunContext,
    StreamPacket,
    parse_approval_decision,
)
from agent_runtime.orchestrator.registry import WorkflowBuilder, WorkflowRegistry
from agent_runtime.orchestrator.state import NodeOutput, WorkflowState
from agent_runtime.protocol.events import (
    AgentFailure,
    InteractionRequestEvent,
    PendingInterrupt,
    PendingInterruptKind,
    RuntimeEvent,
    RuntimeEventPayload,
    TokenUsage,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
    WorkflowResumedEvent,
    WorkflowStartedEvent,
)

_CHECKPOINT_SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=[
        (NodeOutput.__module__, NodeOutput.__qualname__),
        (AgentFailure.__module__, AgentFailure.__qualname__),
        (InteractionRequestEvent.__module__, InteractionRequestEvent.__qualname__),
        (TokenUsage.__module__, TokenUsage.__qualname__),
    ]
)


class ResumeInput(BaseModel):
    """单个挂起分支的恢复输入（用于多分支批量并发恢复）。"""

    model_config = ConfigDict(frozen=True)

    resume_value: Any = None
    interaction_response: InteractionResponse | None = None
    prompt: str | None = None


class ThreadState(BaseModel):
    """线程综合状态视图：合并 LangGraph Checkpoint 与 SQLite 预写表，供重启后还原 UI 与断点恢复。"""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    status: Literal["completed", "interrupted", "aborted", "failed", "crashed", "running"]
    resumable: bool
    node_outputs: dict[str, NodeOutput] = Field(default_factory=dict)
    next_nodes: tuple[str, ...] = ()
    interrupts: tuple[PendingInterrupt, ...] = ()
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
            raw_saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(conn_str))
            checkpointer = AsyncSqliteSaver(raw_saver.conn, serde=_CHECKPOINT_SERDE)
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
                existing_state, _, paused_outputs = await self._inspect(
                    compiled, effective_thread_id, cleanup=True
                )
            except KeyError:
                existing_state, paused_outputs = None, {}

            if existing_state is not None and existing_state.resumable:
                if existing_state.status not in ("failed", "aborted"):
                    raise ValueError(
                        f"Thread '{effective_thread_id}' is {existing_state.status}; "
                        "call resume() or discard()"
                    )
                if paused_outputs:
                    initial_state["node_outputs"] = {
                        node_id: out.model_copy(update={"pending_interaction": None})
                        for node_id, out in paused_outputs.items()
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
        node_id: str | None = None,
        resume_value: Any = None,
        interaction_response: InteractionResponse | None = None,
        prompt: str | None = None,
        resumes: Mapping[str, ResumeInput] | None = None,
        adapter: str = "pi",
        endpoint: ModelEndpoint | None = None,
        thinking_level: ThinkingLevel | None = None,
        persist_session: bool = True,
        interaction_handler: InteractionHandler | None = None,
        interaction_mode: InteractionMode = "cancel",
    ) -> AsyncIterator[RuntimeEvent]:
        """统一断点恢复入口：支持单分支精确恢复与多分支批量并发恢复。"""
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
            state, _, _ = await self._inspect(compiled, thread_id, cleanup=True)
            if not state.resumable:
                raise ValueError(
                    f"Thread '{thread_id}' is already {state.status}; start a new turn with run()"
                )

            graph_input, staged_replies, reason = _resolve_resume_plan(
                state,
                node_id=node_id,
                resume_value=resume_value,
                interaction_response=interaction_response,
                prompt=prompt,
                resumes=resumes,
            )

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
                staged_replies=staged_replies,
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
            state, _, paused_outputs = await self._inspect(compiled, thread_id, cleanup=True)
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
            for paused_node_id, paused in paused_outputs.items():
                node_status: Literal["failed", "aborted"] = (
                    "failed" if paused.status == "failed" else "aborted"
                )
                preserved[paused_node_id] = paused.model_copy(
                    update={"status": node_status, "pending_interaction": None}
                )
            first_failure = (
                next((out.failure for out in paused_outputs.values() if out.failure), None)
                if final_status == "failed"
                else None
            )

            await _close_resumable_turn(
                checkpointer,
                thread_id=thread_id,
                node_outputs=preserved,
                status=final_status,
                failure=first_failure,
            )
            for row in state.inflight_nodes:
                await store.delete(row)

            closed, _, _ = await self._inspect(compiled, thread_id, cleanup=True)
            return closed

    async def respond_interaction(
        self,
        run_id: str,
        response: InteractionResponse,
        *,
        node_id: str | None = None,
    ) -> None:
        """向正在线热等待的节点回传用户审批结果或提问回答。"""
        pending_map = self._run_context.pending_interactions.get(run_id)
        if not pending_map:
            raise KeyError(f"No pending interaction found for run_id '{run_id}'")
        if node_id is not None:
            pending = pending_map.get(node_id)
            if pending is None:
                raise KeyError(
                    f"No pending interaction found for run_id '{run_id}', node_id '{node_id}'"
                )
            if pending.request.request_id != response.request_id:
                raise ValueError(
                    f"Interaction request_id mismatch for run_id '{run_id}' node '{node_id}': "
                    f"expected '{pending.request.request_id}', got '{response.request_id}'"
                )
        else:
            matches = [
                (nid, item)
                for nid, item in pending_map.items()
                if item.request.request_id == response.request_id
            ]
            if len(matches) == 1:
                pending = matches[0][1]
            elif len(matches) > 1:
                nodes = ", ".join(nid for nid, _ in matches)
                raise ValueError(
                    f"Ambiguous request_id '{response.request_id}' across nodes ({nodes}); "
                    "specify node_id"
                )
            else:
                expected = ", ".join(item.request.request_id for item in pending_map.values())
                raise ValueError(
                    f"Interaction request_id mismatch for run_id '{run_id}': "
                    f"expected one of [{expected}], got '{response.request_id}'"
                )
        if not pending.future.done():
            pending.future.set_result(response)

    async def suspend(self, run_id: str) -> None:
        """优雅冷挂起指定活跃运行：落盘全部活跃分支的 session_ref 与待回复交互。"""
        self._run_context.suspended_runs.add(run_id)
        signaled_nodes: set[str] = set()
        pending_map = self._run_context.pending_interactions.get(run_id)
        if pending_map:
            for node_id, pending in list(pending_map.items()):
                if not pending.future.done():
                    pending.future.set_result(SUSPEND_SIGNAL)
                    signaled_nodes.add(node_id)

        sessions = self._run_context.active_sessions.get(run_id)
        if sessions:
            for node_id, session in list(sessions.items()):
                if node_id not in signaled_nodes:
                    with contextlib.suppress(AdapterError, OSError):
                        await session.abort()

    async def suspend_all(self) -> None:
        """优雅冷挂起当前全部活跃运行（供桌面端 before-quit / SIGTERM 退出前调用）。"""
        active_ids = set(self._run_context.active_sessions) | set(
            self._run_context.pending_interactions
        )
        for run_id in list(active_ids):
            await self.suspend(run_id)

    async def steer(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        session = self._require_active_session(run_id, node_id=node_id)
        await session.steer(message)

    async def follow_up(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        session = self._require_active_session(run_id, node_id=node_id)
        await session.follow_up(message)

    async def abort(self, run_id: str) -> None:
        # 若当前正卡在热交互等待期，先以 cancel 唤醒全部等待弹窗再发 abort，防止死锁
        pending_map = self._run_context.pending_interactions.get(run_id)
        if pending_map:
            for pending in list(pending_map.values()):
                if not pending.future.done():
                    pending.future.set_result(
                        InteractionResponse(
                            request_id=pending.request.request_id, decision="cancel"
                        )
                    )
        sessions = self._run_context.active_sessions.get(run_id)
        if not sessions:
            raise KeyError(f"No active agent session found for run_id '{run_id}'")
        for session in list(sessions.values()):
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
    ) -> tuple[ThreadState, StateSnapshot, dict[str, NodeOutput]]:
        store = self._require_inflight_store()
        records = await store.list_for_thread(thread_id)
        snapshot = await compiled.aget_state(
            {"configurable": {"thread_id": thread_id}}, subgraphs=True
        )
        if not snapshot.values and not snapshot.next:
            raise KeyError(f"No checkpoint found for thread_id '{thread_id}'")

        outputs = dict(snapshot.values.get("node_outputs", {}))
        raw_status = snapshot.values.get("status")
        live: list[InflightNodeRecord] = []
        paused_outputs: dict[str, NodeOutput] = {}
        interrupts: list[PendingInterrupt] = []
        pending_nodes = set(snapshot.next)
        seen_pending: set[str] = set()

        for task in snapshot.tasks:
            if task.result is not None or task.name not in pending_nodes:
                continue
            seen_pending.add(task.name)
            child = task.state
            if isinstance(child, StateSnapshot):
                if child.next == ("await_resume",):
                    paused = child.values.get("node_outputs", {}).get(task.name)
                    if paused is not None:
                        paused_outputs[paused.node_id] = paused
                        outputs[paused.node_id] = paused
                        intr_id = task.interrupts[0].id if task.interrupts else None
                        if paused.status == "interrupted":
                            kind: PendingInterruptKind = (
                                "agent_interaction" if paused.pending_interaction else "suspended"
                            )
                        else:
                            kind = paused.status
                        interrupts.append(
                            PendingInterrupt(
                                interrupt_id=intr_id,
                                node_id=paused.node_id,
                                kind=kind,
                                interaction=paused.pending_interaction,
                                failure=paused.failure,
                            )
                        )
                        continue

                pending_ids = {step.id for step in child.tasks}
                matched_wal = False
                for row in records:
                    if row.node_id != task.name or row.task_id not in pending_ids:
                        continue
                    matched_wal = True
                    live.append(row)
                    existing = outputs.get(task.name)
                    outputs[task.name] = NodeOutput(
                        node_id=task.name,
                        role=row.role,
                        status="interrupted",
                        summary="",
                        session_ref=row.session_ref
                        or (existing.session_ref if existing is not None else None),
                        pending_interaction=row.pending_interaction,
                    )
                    interrupts.append(
                        PendingInterrupt(
                            interrupt_id=None,
                            node_id=task.name,
                            kind="crashed",
                            interaction=row.pending_interaction,
                        )
                    )
                if not matched_wal:
                    interrupts.append(
                        PendingInterrupt(interrupt_id=None, node_id=task.name, kind="crashed")
                    )
                continue

            if task.interrupts:
                for intr in task.interrupts:
                    raw_payload = _extract_interrupt_payload((intr,))
                    raw_interaction = raw_payload.get("interaction")
                    if isinstance(raw_interaction, InteractionRequestEvent):
                        interaction: InteractionRequestEvent | None = raw_interaction
                        payload = {k: v for k, v in raw_payload.items() if k != "interaction"}
                    else:
                        interaction = None
                        payload = raw_payload
                    interrupts.append(
                        PendingInterrupt(
                            interrupt_id=intr.id,
                            node_id=task.name,
                            kind="workflow_interrupt",
                            interaction=interaction,
                            payload=payload,
                        )
                    )
            else:
                interrupts.append(
                    PendingInterrupt(interrupt_id=None, node_id=task.name, kind="crashed")
                )

        for node_name in snapshot.next:
            if node_name not in seen_pending:
                interrupts.append(
                    PendingInterrupt(interrupt_id=None, node_id=node_name, kind="crashed")
                )

        if cleanup:
            for row in records:
                if row not in live:
                    await store.delete(row)

        if interrupts:
            kinds = {item.kind for item in interrupts}
            if "crashed" in kinds:
                status: Literal["completed", "interrupted", "aborted", "failed", "crashed"] = (
                    "crashed"
                )
            elif kinds & {"workflow_interrupt", "agent_interaction", "suspended"}:
                status = "interrupted"
            elif "failed" in kinds:
                status = "failed"
            else:
                status = "aborted"
        elif raw_status == "running":
            status = "completed"
        else:
            status = raw_status

        view = ThreadState(
            thread_id=thread_id,
            status=status,
            resumable=bool(snapshot.next),
            node_outputs=outputs,
            next_nodes=snapshot.next,
            interrupts=tuple(interrupts),
            inflight_nodes=tuple(live),
        )
        return view, snapshot, paused_outputs

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
        staged_replies: dict[str, Any] | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        seq = 0
        pending_replies = dict(staged_replies) if staged_replies else {}

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
                state, snapshot, _ = await self._inspect(compiled, thread_id, cleanup=True)
                if not pending_replies:
                    break
                interrupt_ids_by_node = {
                    item.node_id: item.interrupt_id
                    for item in state.interrupts
                    if item.interrupt_id is not None
                }
                resume_map: dict[str, Any] = {}
                for target_node, reply_value in pending_replies.items():
                    intr_id = interrupt_ids_by_node.get(target_node)
                    if intr_id is None:
                        raise RuntimeError(
                            f"Expected recovered node '{target_node}' to reach its resume gate"
                        )
                    resume_map[intr_id] = reply_value
                graph_input = Command(resume=resume_map)
                pending_replies = {}

            if state.status == "interrupted":
                event_node_id = state.interrupts[0].node_id if len(state.interrupts) == 1 else None
                yield _wrap(
                    WorkflowInterruptedEvent(interrupts=state.interrupts),
                    node_id=event_node_id,
                )
            elif state.status in ("failed", "aborted", "completed"):
                first_failure = next(
                    (item.failure for item in state.interrupts if item.failure is not None),
                    snapshot.values.get("failure"),
                )
                yield _wrap(
                    WorkflowCompletedEvent(
                        status=state.status,
                        final_output=snapshot.values.get("latest_output", ""),
                        failure=first_failure,
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

    def _require_active_session(self, run_id: str, *, node_id: str | None = None) -> AgentSession:
        sessions = self._run_context.active_sessions.get(run_id)
        if not sessions:
            raise KeyError(f"No active agent session found for run_id '{run_id}'")
        if node_id is not None:
            session = sessions.get(node_id)
            if session is None:
                raise KeyError(
                    f"No active agent session found for run_id '{run_id}', node_id '{node_id}'"
                )
            return session
        if len(sessions) == 1:
            return next(iter(sessions.values()))
        nodes = ", ".join(sorted(sessions))
        raise ValueError(
            f"Multiple active agent sessions for run_id '{run_id}' ({nodes}); specify node_id"
        )


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


def _resolve_resume_plan(
    state: ThreadState,
    *,
    node_id: str | None,
    resume_value: Any,
    interaction_response: InteractionResponse | None,
    prompt: str | None,
    resumes: Mapping[str, ResumeInput] | None,
) -> tuple[
    Any,
    dict[str, Any],
    Literal["workflow_interrupt", "agent_interaction", "failure", "crash"],
]:
    """将单分支或批量恢复参数解析为 LangGraph `Command(resume=...)` 或崩溃两阶段回放计划。"""
    targets: list[tuple[PendingInterrupt, ResumeInput]] = []
    if resumes is not None:
        if (
            node_id is not None
            or resume_value is not None
            or interaction_response is not None
            or prompt is not None
        ):
            raise ValueError("Cannot combine 'resumes' with single-branch resume parameters")
        if not resumes:
            raise ValueError("'resumes' must specify at least one target node_id")
        for target_node, inp in resumes.items():
            matches = [item for item in state.interrupts if item.node_id == target_node]
            if not matches:
                pending = ", ".join(item.node_id for item in state.interrupts)
                raise ValueError(f"Node '{target_node}' is not paused; pending nodes: [{pending}]")
            if len(matches) > 1:
                raise ValueError(f"Node '{target_node}' has multiple active interrupts")
            targets.append((matches[0], inp))
    else:
        single = ResumeInput(
            resume_value=resume_value,
            interaction_response=interaction_response,
            prompt=prompt,
        )
        if node_id is not None:
            matches = [item for item in state.interrupts if item.node_id == node_id]
            if not matches:
                pending = ", ".join(item.node_id for item in state.interrupts)
                raise ValueError(f"Node '{node_id}' is not paused; pending nodes: [{pending}]")
            if len(matches) > 1:
                raise ValueError(f"Node '{node_id}' has multiple active interrupts")
            targets.append((matches[0], single))
        elif len(state.interrupts) == 1:
            targets.append((state.interrupts[0], single))
        elif interaction_response is not None:
            matches = [
                item
                for item in state.interrupts
                if item.interaction is not None
                and item.interaction.request_id == interaction_response.request_id
            ]
            if len(matches) == 1:
                targets.append((matches[0], single))
            elif len(matches) > 1:
                nodes = ", ".join(item.node_id for item in matches)
                raise ValueError(
                    f"Multiple pending nodes share request_id "
                    f"'{interaction_response.request_id}' ({nodes}); specify node_id"
                )
            else:
                raise ValueError("Interaction request_id does not match any pending request")
        elif (
            all(item.kind == "crashed" for item in state.interrupts)
            and not state.inflight_nodes
            and resume_value is None
            and not (prompt and prompt.strip())
        ):
            targets = [(item, single) for item in state.interrupts]
        else:
            pending = ", ".join(item.node_id for item in state.interrupts)
            raise ValueError(
                f"Multiple nodes are paused ({pending}); specify node_id, "
                "a matching interaction_response, or resumes"
            )

    wal_nodes = {row.node_id for row in state.inflight_nodes}
    needs_crash_replay = state.status == "crashed"
    resume_map: dict[str, Any] = {}
    staged_replies: dict[str, Any] = {}

    for item, inp in targets:
        if item.kind == "workflow_interrupt":
            if item.interaction is not None:
                # Validate structured approval input before LangGraph persists __resume__ writes.
                if inp.interaction_response is not None:
                    if inp.interaction_response.request_id != item.interaction.request_id:
                        raise ValueError(
                            "Interaction request_id does not match the pending request"
                        )
                    raw_decision: Any = inp.interaction_response.model_dump()
                elif inp.resume_value is not None:
                    raw_decision = inp.resume_value
                else:
                    raw_decision = inp.prompt
                parsed_decision, parsed_feedback = parse_approval_decision(
                    raw_decision, node_id=item.node_id
                )
                decision: Any = {"decision": parsed_decision, "feedback": parsed_feedback}
            else:
                decision = (
                    inp.resume_value
                    if inp.resume_value is not None
                    else (
                        inp.interaction_response.model_dump()
                        if inp.interaction_response is not None
                        else inp.prompt
                    )
                )
                if decision is None:
                    raise ValueError(
                        f"Workflow interrupt on '{item.node_id}' requires a resume value"
                    )
            if needs_crash_replay:
                staged_replies[item.node_id] = decision
            else:
                assert item.interrupt_id is not None
                resume_map[item.interrupt_id] = decision
        elif item.kind == "crashed":
            text = _format_resume_prompt(
                pending_interaction=item.interaction,
                interaction_response=inp.interaction_response,
                prompt=inp.prompt,
            )
            if item.node_id in wal_nodes:
                staged_replies[item.node_id] = {"prompt": text}
            elif text is not None:
                raise ValueError(
                    "Cannot pass a continuation prompt when resuming a crash with no started "
                    "agent turn"
                )
        else:
            text = _format_resume_prompt(
                pending_interaction=item.interaction,
                interaction_response=inp.interaction_response,
                prompt=inp.prompt,
            )
            payload = {"prompt": text}
            if needs_crash_replay:
                staged_replies[item.node_id] = payload
            else:
                assert item.interrupt_id is not None
                resume_map[item.interrupt_id] = payload

    target_kinds = {item.kind for item, _ in targets}
    if needs_crash_replay or "crashed" in target_kinds:
        reason: Literal["workflow_interrupt", "agent_interaction", "failure", "crash"] = "crash"
    elif "workflow_interrupt" in target_kinds:
        reason = "workflow_interrupt"
    elif target_kinds & {"agent_interaction", "suspended"}:
        reason = "agent_interaction"
    else:
        reason = "failure"

    graph_input: Any = None if needs_crash_replay else Command(resume=resume_map)
    return graph_input, staged_replies, reason


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
