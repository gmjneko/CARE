"""WorkflowRunner: compiles LangGraph workflows with SQLite checkpoints and event streams."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
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
    status: Literal["completed", "interrupted", "aborted", "failed", "crashed"]
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
            "resume_mode": False,
            "resume_prompt": None,
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
        inflight_store = self._require_inflight_store()
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

        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await compiled.aget_state(config)
        inflight_records = await inflight_store.list_for_thread(thread_id)
        prev_values = dict(snapshot.values) if snapshot.values else {}

        if not prev_values and not inflight_records:
            raise KeyError(f"No checkpoint found for thread_id '{thread_id}'")

        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"

        # 判定 1：LangGraph 节点间主动 interrupt() 挂起 -> Command(resume=...) 精确恢复
        if snapshot.interrupts:
            effective_resume_value = (
                resume_value
                if resume_value is not None
                else (
                    interaction_response.model_dump()
                    if interaction_response is not None
                    else prompt
                )
            )
            async for event in self._stream_execution(
                compiled=compiled,
                workflow_name=workflow_name,
                run_id=effective_run_id,
                thread_id=thread_id,
                graph_input=Command(resume=effective_resume_value),
                opening_event=WorkflowResumedEvent(
                    preset=workflow_name,
                    thread_id=thread_id,
                    resumed_from="workflow_interrupt",
                ),
                interaction_handler=interaction_handler,
                interaction_mode=interaction_mode,
            ):
                yield event
            return

        prev_node_outputs: dict[str, NodeOutput] = dict(prev_values.get("node_outputs") or {})

        # 判定 2：存在 WAL 预写残留或 snapshot.next 非空（进程遭 kill -9 / 未捕获异常硬崩溃）
        if inflight_records or snapshot.next:
            wal_outputs, wal_interaction = _synthesize_wal_outputs(
                prev_node_outputs, inflight_records
            )
            resume_prompt = _format_resume_prompt(
                pending_interaction=wal_interaction,
                interaction_response=interaction_response,
                prompt=prompt,
            )
            if snapshot.next:
                # 直接更新断点状态并 astream(None)，跳过已执行上游节点、精确落回崩溃节点
                await compiled.aupdate_state(
                    config,
                    {
                        "run_id": effective_run_id,
                        "thread_id": thread_id,
                        "node_outputs": wal_outputs,
                        "status": "running",
                        "failure": None,
                        "resume_mode": True,
                        "resume_prompt": resume_prompt,
                    },
                )
                graph_input: Any = None
            else:
                graph_input = _build_resumed_state(
                    prev_values=prev_values,
                    workflow_name=workflow_name,
                    run_id=effective_run_id,
                    thread_id=thread_id,
                    node_outputs={**prev_node_outputs, **wal_outputs},
                    resume_prompt=resume_prompt,
                )

            async for event in self._stream_execution(
                compiled=compiled,
                workflow_name=workflow_name,
                run_id=effective_run_id,
                thread_id=thread_id,
                graph_input=graph_input,
                opening_event=WorkflowResumedEvent(
                    preset=workflow_name,
                    thread_id=thread_id,
                    resumed_from="crash",
                ),
                interaction_handler=interaction_handler,
                interaction_mode=interaction_mode,
            ):
                yield event
            return

        # 判定 3：上次终态为 interrupted / aborted / failed -> 跳过已完成上游节点，从断点续跑
        prev_status = prev_values.get("status")
        if prev_status not in ("interrupted", "aborted", "failed"):
            raise ValueError(
                f"Thread '{thread_id}' is already completed; "
                f"call run(..., thread_id='{thread_id}') for a follow-up turn."
            )

        pending_interaction = _find_pending_interaction(prev_node_outputs)
        resumed_from: Literal["agent_interaction", "failure"] = (
            "agent_interaction" if prev_status == "interrupted" else "failure"
        )
        resume_prompt = _format_resume_prompt(
            pending_interaction=pending_interaction,
            interaction_response=interaction_response,
            prompt=prompt,
        )
        resumed_state = _build_resumed_state(
            prev_values=prev_values,
            workflow_name=workflow_name,
            run_id=effective_run_id,
            thread_id=thread_id,
            node_outputs=prev_node_outputs,
            resume_prompt=resume_prompt,
        )
        async for event in self._stream_execution(
            compiled=compiled,
            workflow_name=workflow_name,
            run_id=effective_run_id,
            thread_id=thread_id,
            graph_input=resumed_state,
            opening_event=WorkflowResumedEvent(
                preset=workflow_name,
                thread_id=thread_id,
                resumed_from=resumed_from,
            ),
            interaction_handler=interaction_handler,
            interaction_mode=interaction_mode,
        ):
            yield event

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
        inflight_store = self._require_inflight_store()
        _, builder = self._resolve_workflow(workflow)
        compiled = _compile_workflow(builder, self._run_context, checkpointer=checkpointer)
        snapshot = await compiled.aget_state({"configurable": {"thread_id": thread_id}})
        inflight_records = await inflight_store.list_for_thread(thread_id)
        values = dict(snapshot.values) if snapshot.values else {}

        if not values and not inflight_records:
            raise KeyError(f"No checkpoint found for thread_id '{thread_id}'")

        node_outputs: dict[str, NodeOutput] = dict(values.get("node_outputs") or {})
        next_nodes = tuple(snapshot.next)

        if snapshot.interrupts:
            interrupt_payload = _extract_interrupt_payload(snapshot.interrupts)
            return ThreadState(
                thread_id=thread_id,
                status="interrupted",
                resumable=True,
                node_outputs=node_outputs,
                next_nodes=next_nodes,
                interrupted_node_id=next_nodes[0] if next_nodes else None,
                interrupt_payload=interrupt_payload,
                inflight_nodes=tuple(inflight_records),
            )

        if inflight_records or next_nodes:
            wal_outputs, wal_interaction = _synthesize_wal_outputs(node_outputs, inflight_records)
            merged = {**node_outputs, **wal_outputs}
            interrupted_node = (
                inflight_records[-1].node_id
                if inflight_records
                else (next_nodes[0] if next_nodes else None)
            )
            return ThreadState(
                thread_id=thread_id,
                status="crashed",
                resumable=True,
                node_outputs=merged,
                next_nodes=next_nodes,
                pending_interaction=wal_interaction,
                interrupted_node_id=interrupted_node,
                inflight_nodes=tuple(inflight_records),
            )

        raw_status = values.get("status", "completed")
        if raw_status == "interrupted":
            interrupted_node, pending_interaction = _find_interrupted_node(node_outputs)
            return ThreadState(
                thread_id=thread_id,
                status="interrupted",
                resumable=True,
                node_outputs=node_outputs,
                pending_interaction=pending_interaction,
                interrupted_node_id=interrupted_node,
            )

        if raw_status in ("aborted", "failed"):
            return ThreadState(
                thread_id=thread_id,
                status=raw_status,
                resumable=True,
                node_outputs=node_outputs,
            )

        return ThreadState(
            thread_id=thread_id,
            status="completed",
            resumable=False,
            node_outputs=node_outputs,
        )

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
    ) -> AsyncIterator[RuntimeEvent]:
        inflight_store = self._require_inflight_store()
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
        self._run_context.active_run_ids[thread_id] = run_id

        last_status: Literal["running", "interrupted", "completed", "failed", "aborted"] = "running"
        last_output = ""
        last_failure: AgentFailure | None = None
        langgraph_interrupt_payload: dict[str, Any] | None = None
        interrupted_node_id: str | None = None
        interrupted_interaction: InteractionRequestEvent | None = None

        try:
            yield _wrap(opening_event)

            async for chunk in compiled.astream(
                graph_input,
                config={"configurable": {"thread_id": thread_id}},
                stream_mode=["custom", "updates"],
                version="v2",
            ):
                chunk_dict = cast(dict[str, Any], chunk)
                chunk_type = chunk_dict.get("type")
                data = chunk_dict.get("data")

                if chunk_type == "custom" and isinstance(data, StreamPacket):
                    yield _wrap(data.event, node_id=data.node_id)
                elif chunk_type == "updates" and isinstance(data, dict):
                    if "__interrupt__" in data:
                        langgraph_interrupt_payload = _extract_interrupt_payload(
                            data["__interrupt__"]
                        )
                    for updated_node_id, node_update in data.items():
                        if updated_node_id == "__interrupt__" or not isinstance(node_update, dict):
                            continue
                        # 节点更新已由 LangGraph 提交进 Checkpoint，此时安全清除对应预写记录
                        await inflight_store.delete(thread_id, updated_node_id)
                        if "status" in node_update:
                            last_status = node_update["status"]
                        if "latest_output" in node_update:
                            last_output = node_update["latest_output"]
                        if "failure" in node_update:
                            last_failure = node_update["failure"]
                        if "node_outputs" in node_update and isinstance(
                            node_update["node_outputs"], dict
                        ):
                            for out_id, out in node_update["node_outputs"].items():
                                if isinstance(out, NodeOutput) and out.status == "interrupted":
                                    interrupted_node_id = out_id
                                    interrupted_interaction = out.pending_interaction

            if langgraph_interrupt_payload is not None:
                snap = await compiled.aget_state({"configurable": {"thread_id": thread_id}})
                paused_node = snap.next[0] if snap.next else None
                yield _wrap(
                    WorkflowInterruptedEvent(
                        reason="workflow_interrupt",
                        node_id=paused_node,
                        payload=langgraph_interrupt_payload,
                    ),
                    node_id=paused_node,
                )
            elif last_status == "interrupted":
                reason: Literal["agent_interaction", "suspended"] = (
                    "agent_interaction" if interrupted_interaction is not None else "suspended"
                )
                yield _wrap(
                    WorkflowInterruptedEvent(
                        reason=reason,
                        node_id=interrupted_node_id,
                        interaction=interrupted_interaction,
                    ),
                    node_id=interrupted_node_id,
                )
            else:
                final_status: Literal["completed", "failed", "aborted"] = (
                    "completed" if last_status == "running" else last_status
                )
                yield _wrap(
                    WorkflowCompletedEvent(
                        status=final_status,
                        final_output=last_output,
                        failure=last_failure,
                    )
                )
        finally:
            if self._run_context.active_run_ids.get(thread_id) == run_id:
                del self._run_context.active_run_ids[thread_id]
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
    """编译工作流图，兼容未声明 persist_session 参数的自定义 WorkflowBuilder。"""
    try:
        graph = builder(
            run_context,
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
            persist_session=persist_session,
        )
    except TypeError:
        graph = builder(
            run_context,
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
        )
    return graph.compile(checkpointer=checkpointer)


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


def _synthesize_wal_outputs(
    prev_outputs: dict[str, NodeOutput],
    inflight_records: list[InflightNodeRecord],
) -> tuple[dict[str, NodeOutput], InteractionRequestEvent | None]:
    """将残留 WAL 预写记录转换为未完成节点的 NodeOutput，提取最近待回复弹窗。"""
    wal_outputs: dict[str, NodeOutput] = {}
    latest_interaction: InteractionRequestEvent | None = None
    for rec in inflight_records:
        existing = prev_outputs.get(rec.node_id)
        if existing is not None and existing.status == "completed":
            continue
        wal_outputs[rec.node_id] = NodeOutput(
            node_id=rec.node_id,
            role=existing.role if existing is not None else rec.node_id,
            status="interrupted",
            summary=existing.summary if existing is not None else "",
            session_ref=rec.session_ref or (existing.session_ref if existing is not None else None),
            pending_interaction=rec.pending_interaction,
        )
        if rec.pending_interaction is not None:
            latest_interaction = rec.pending_interaction
    return wal_outputs, latest_interaction


def _find_interrupted_node(
    node_outputs: dict[str, NodeOutput],
) -> tuple[str | None, InteractionRequestEvent | None]:
    for node_id, output in node_outputs.items():
        if output.status == "interrupted":
            return node_id, output.pending_interaction
    return None, None


def _find_pending_interaction(
    node_outputs: dict[str, NodeOutput],
) -> InteractionRequestEvent | None:
    _, interaction = _find_interrupted_node(node_outputs)
    return interaction


def _format_resume_prompt(
    *,
    pending_interaction: InteractionRequestEvent | None,
    interaction_response: InteractionResponse | None,
    prompt: str | None,
) -> str | None:
    """将冷恢复时的用户审批裁决或问题回答格式化为续接指令（同时支持直接传入补充 prompt）。"""
    parts: list[str] = []
    if interaction_response is not None:
        title = pending_interaction.title if pending_interaction is not None else "request"
        if pending_interaction is not None and pending_interaction.kind == "user_input":
            if interaction_response.decision == "cancel":
                parts.append(f"The user cancelled the question ({title}).")
            else:
                answer = interaction_response.value or ""
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


def _build_resumed_state(
    *,
    prev_values: dict[str, Any],
    workflow_name: str,
    run_id: str,
    thread_id: str,
    node_outputs: dict[str, NodeOutput],
    resume_prompt: str | None,
) -> WorkflowState:
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "workflow_id": workflow_name,
        "prompt": str(prev_values.get("prompt", "")),
        "workspace_dir": str(prev_values.get("workspace_dir", ".")),
        "node_outputs": node_outputs,
        "latest_output": str(prev_values.get("latest_output", "")),
        "status": "running",
        "failure": None,
        "resume_mode": True,
        "resume_prompt": resume_prompt,
    }
