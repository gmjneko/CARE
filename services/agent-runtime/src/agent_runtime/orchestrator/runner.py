"""WorkflowRunner: executes compiled LangGraph workflows with runtime context and event streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END
from langgraph.types import Command

from agent_runtime.adapters.base import ModelEndpoint, ThinkingLevel
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.orchestrator.live_runs import (
    ExecutionContext,
    InteractionHandler,
    InteractionMode,
    LiveRunRegistry,
    NodeRuntimeOverride,
    RunOptions,
)
from agent_runtime.orchestrator.node import StreamPacket
from agent_runtime.orchestrator.persistence import (
    InflightNodeStore,
    PersistenceBackend,
    SqlitePersistenceBackend,
)
from agent_runtime.orchestrator.recovery import (
    ResumeInput,
    ThreadState,
    inspect_thread,
    resolve_resume_plan,
)
from agent_runtime.orchestrator.registry import WorkflowBuilder, WorkflowRegistry
from agent_runtime.orchestrator.state import NodeOutput
from agent_runtime.protocol.events import (
    InteractionResponse,
    RuntimeEvent,
    RuntimeEventPayload,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
    WorkflowResumedEvent,
    WorkflowStartedEvent,
)

# Unreachable terminal sink attached on compile so discard() closes checkpoints via aupdate_state().
_CLOSE_NODE = "_care_close"


class WorkflowRunner:
    """Production execution engine for code-defined LangGraph Agent workflows."""

    def __init__(
        self,
        adapter_registry: AdapterRegistry,
        workflow_registry: WorkflowRegistry | None = None,
        *,
        persistence: PersistenceBackend | None = None,
    ) -> None:
        self._adapters = adapter_registry
        self._workflow_registry = workflow_registry or WorkflowRegistry()
        self._persistence: PersistenceBackend = persistence or SqlitePersistenceBackend(":memory:")
        self._live_runs = LiveRunRegistry()
        self._compiled_cache: dict[str, Any] = {}
        self._entered = False

    async def __aenter__(self) -> WorkflowRunner:
        await self._persistence.__aenter__()
        self._entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._entered = False
        self._compiled_cache.clear()
        await self._persistence.__aexit__(exc_type, exc, tb)

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
        node_overrides: Mapping[str, NodeRuntimeOverride] | None = None,
        interaction_handler: InteractionHandler | None = None,
        interaction_mode: InteractionMode = "cancel",
    ) -> AsyncIterator[RuntimeEvent]:
        """发起一次新工作流运行，或携带既有 `thread_id` 进行连续多轮对话。"""
        compiled, workflow_name = self._get_compiled(workflow)
        inflight_store = self._require_inflight_store()

        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"
        effective_thread_id = thread_id or effective_run_id

        options = RunOptions(
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
            persist_session=persist_session,
            node_overrides=dict(node_overrides) if node_overrides else {},
        )

        with self._live_runs.claim(effective_thread_id, effective_run_id):
            try:
                inspected = await inspect_thread(
                    compiled, inflight_store, effective_thread_id, cleanup=True
                )
            except KeyError:
                inspected = None

            preserved_sessions: dict[str, str] = {}
            if inspected is not None and inspected.state.resumable:
                if inspected.state.status not in ("failed", "aborted"):
                    raise ValueError(
                        f"Thread '{effective_thread_id}' is {inspected.state.status}; "
                        "call resume() or discard()"
                    )
                if inspected.paused_sessions:
                    preserved_sessions = dict(inspected.paused_sessions)

            # BinaryOperatorAggregate skips the reducer on the very first write to an empty
            # channel, so pass {} / "running" for fresh threads and None to trigger reset on
            # existing threads.
            has_outputs = inspected is not None and "node_outputs" in inspected.snapshot.values
            has_status = inspected is not None and "status" in inspected.snapshot.values
            initial_state: dict[str, Any] = {
                "run_id": effective_run_id,
                "thread_id": effective_thread_id,
                "workflow_id": workflow_name,
                "prompt": prompt,
                "workspace_dir": str(Path(workspace_dir).resolve()),
                "node_outputs": None if has_outputs else {},
                "node_sessions": preserved_sessions,
                "latest_output": "",
                "status": None if has_status else "running",
                "failure": None,
            }

            exec_ctx = ExecutionContext(
                run_id=effective_run_id,
                thread_id=effective_thread_id,
                options=options,
                adapters=self._adapters,
                live_runs=self._live_runs,
                inflight_store=inflight_store,
            )

            async for event in self._stream_execution(
                compiled=compiled,
                workflow_name=workflow_name,
                exec_ctx=exec_ctx,
                graph_input=initial_state,
                opening_event=WorkflowStartedEvent(prompt=prompt),
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
        complete: bool = False,
        resumes: Mapping[str, ResumeInput] | None = None,
        adapter: str = "pi",
        endpoint: ModelEndpoint | None = None,
        thinking_level: ThinkingLevel | None = None,
        persist_session: bool = True,
        node_overrides: Mapping[str, NodeRuntimeOverride] | None = None,
        interaction_handler: InteractionHandler | None = None,
        interaction_mode: InteractionMode = "cancel",
    ) -> AsyncIterator[RuntimeEvent]:
        """统一断点恢复入口：支持单分支精确恢复与多分支批量并发恢复。"""
        compiled, workflow_name = self._get_compiled(workflow)
        inflight_store = self._require_inflight_store()
        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"

        options = RunOptions(
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
            persist_session=persist_session,
            node_overrides=dict(node_overrides) if node_overrides else {},
        )

        with self._live_runs.claim(thread_id, effective_run_id):
            inspected = await inspect_thread(compiled, inflight_store, thread_id, cleanup=True)
            if not inspected.state.resumable:
                raise ValueError(
                    f"Thread '{thread_id}' is already {inspected.state.status}; "
                    "start a new turn with run()"
                )

            plan = resolve_resume_plan(
                inspected.state,
                node_id=node_id,
                resume_value=resume_value,
                interaction_response=interaction_response,
                prompt=prompt,
                complete=complete,
                resumes=resumes,
            )

            exec_ctx = ExecutionContext(
                run_id=effective_run_id,
                thread_id=thread_id,
                options=options,
                adapters=self._adapters,
                live_runs=self._live_runs,
                inflight_store=inflight_store,
            )

            async for event in self._stream_execution(
                compiled=compiled,
                workflow_name=workflow_name,
                exec_ctx=exec_ctx,
                graph_input=plan.graph_input,
                opening_event=WorkflowResumedEvent(resumed_from=plan.reason),
                interaction_handler=interaction_handler,
                interaction_mode=interaction_mode,
                staged_replies=plan.staged_replies,
            ):
                yield event

    async def discard(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        """放弃未完成的挂起/崩溃/失败轮次，保留全部分支的 session_ref 并收口为不可恢复终态。"""
        compiled, _ = self._get_compiled(workflow)
        store = self._require_inflight_store()

        with self._live_runs.claim(thread_id, f"discard-{uuid4().hex[:12]}"):
            inspected = await inspect_thread(compiled, store, thread_id, cleanup=True)
            state = inspected.state
            if not state.resumable:
                return state

            final_status: Literal["failed", "aborted"] = (
                "failed" if state.status == "failed" else "aborted"
            )
            preserved_outputs: dict[str, NodeOutput] = {}
            for row in state.inflight_nodes:
                existing = state.node_outputs.get(row.node_id)
                if existing is not None:
                    preserved_outputs[row.node_id] = existing.model_copy(
                        update={"status": "aborted", "pending_interaction": None}
                    )
            for paused_node_id, paused in inspected.paused_outputs.items():
                node_status: Literal["failed", "aborted"] = (
                    "failed" if paused.status == "failed" else "aborted"
                )
                preserved_outputs[paused_node_id] = paused.model_copy(
                    update={"status": node_status, "pending_interaction": None}
                )
            first_failure = (
                next(
                    (out.failure for out in inspected.paused_outputs.values() if out.failure),
                    None,
                )
                if final_status == "failed"
                else None
            )

            await compiled.aupdate_state(
                {"configurable": {"thread_id": thread_id}},
                {
                    "node_outputs": preserved_outputs,
                    "node_sessions": dict(inspected.paused_sessions),
                    "status": final_status,
                    "failure": first_failure,
                },
                as_node=_CLOSE_NODE,
            )
            for row in state.inflight_nodes:
                await store.delete(row)

            closed = await inspect_thread(compiled, store, thread_id, cleanup=True)
            return closed.state

    async def respond_interaction(
        self,
        run_id: str,
        response: InteractionResponse,
        *,
        node_id: str | None = None,
    ) -> None:
        await self._live_runs.respond_interaction(run_id, response, node_id=node_id)

    async def suspend(self, run_id: str) -> None:
        await self._live_runs.suspend(run_id)

    async def suspend_all(self) -> None:
        await self._live_runs.suspend_all()

    async def steer(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        await self._live_runs.steer(run_id, message, node_id=node_id)

    async def follow_up(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        await self._live_runs.follow_up(run_id, message, node_id=node_id)

    async def abort(self, run_id: str) -> None:
        await self._live_runs.abort(run_id)

    async def get_checkpoint_state(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> dict[str, Any]:
        compiled, _ = self._get_compiled(workflow)
        snapshot = await compiled.aget_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values)

    async def get_thread_state(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        """纯只读查询线程综合状态（合并 LangGraph Checkpoint 与 WAL 预写表）。"""
        compiled, _ = self._get_compiled(workflow)
        inspected = await inspect_thread(compiled, self._require_inflight_store(), thread_id)
        if self._live_runs.is_thread_running(thread_id):
            return inspected.state.model_copy(update={"status": "running", "resumable": False})
        return inspected.state

    async def _stream_execution(
        self,
        *,
        compiled: Any,
        workflow_name: str,
        exec_ctx: ExecutionContext,
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
                run_id=exec_ctx.run_id,
                thread_id=exec_ctx.thread_id,
                workflow_id=workflow_name,
                node_id=node_id,
                payload=payload,
            )

        self._live_runs.configure_run(
            exec_ctx.run_id,
            interaction_handler=interaction_handler,
            interaction_mode=interaction_mode,
        )

        try:
            yield _wrap(opening_event)
            while True:
                async for chunk in compiled.astream(
                    graph_input,
                    config={"configurable": {"thread_id": exec_ctx.thread_id}},
                    context=exec_ctx,
                    stream_mode="custom",
                    subgraphs=True,
                    version="v2",
                    durability="sync",
                ):
                    packet = chunk["data"]
                    if isinstance(packet, StreamPacket):
                        yield _wrap(packet.event, node_id=packet.node_id)

                inspected = await inspect_thread(
                    compiled, exec_ctx.inflight_store, exec_ctx.thread_id, cleanup=True
                )
                state = inspected.state
                snapshot = inspected.snapshot
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
            self._live_runs.cleanup_run(exec_ctx.run_id)

    def _get_compiled(self, workflow: str | WorkflowBuilder) -> tuple[Any, str]:
        checkpointer = self._require_checkpointer()
        if isinstance(workflow, str):
            cached = self._compiled_cache.get(workflow)
            if cached is not None:
                return cached, workflow
            definition = self._workflow_registry.get(workflow)
            compiled = _compile_workflow(definition.build, checkpointer)
            self._compiled_cache[definition.name] = compiled
            return compiled, definition.name
        name = getattr(workflow, "__name__", "custom_workflow")
        return _compile_workflow(workflow, checkpointer), name

    def _require_checkpointer(self) -> BaseCheckpointSaver[Any]:
        if not self._entered:
            raise RuntimeError(
                "WorkflowRunner must be used inside 'async with WorkflowRunner(...)'"
            )
        return self._persistence.checkpointer

    def _require_inflight_store(self) -> InflightNodeStore:
        if not self._entered:
            raise RuntimeError(
                "WorkflowRunner must be used inside 'async with WorkflowRunner(...)'"
            )
        return self._persistence.inflight_store


def _compile_workflow(
    builder: WorkflowBuilder,
    checkpointer: BaseCheckpointSaver[Any],
) -> Any:
    graph = builder()
    if _CLOSE_NODE not in graph.nodes:
        graph.add_node(_CLOSE_NODE, lambda _state: {})
        graph.add_edge(_CLOSE_NODE, END)
    return graph.compile(checkpointer=checkpointer)
