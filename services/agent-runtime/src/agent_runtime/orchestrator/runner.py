"""WorkflowRunner: executes compiled LangGraph workflows with runtime context and event streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END
from langgraph.types import Command
from pydantic import SecretStr

from agent_runtime.adapters.base import ModelEndpoint, ThinkingLevel
from agent_runtime.adapters.errors import AdapterError
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.errors import InvalidInputError, NotFoundError, StateConflictError
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
    ThreadHistoryStore,
    ThreadSummary,
)
from agent_runtime.orchestrator.recovery import (
    ResumeInput,
    ThreadState,
    inspect_thread,
    resolve_resume_plan,
)
from agent_runtime.orchestrator.registry import WorkflowBuilder, WorkflowRegistry
from agent_runtime.orchestrator.state import (
    NodeOutput,
    PersistedEndpoint,
    PersistedNodeOverride,
    PersistedRunOptions,
)
from agent_runtime.protocol.events import (
    InteractionResponse,
    PendingInterrupt,
    RuntimeErrorEvent,
    RuntimeEvent,
    RuntimeEventPayload,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
    WorkflowResumedEvent,
    WorkflowStartedEvent,
)

# Unreachable terminal sink attached on compile so discard() closes checkpoints via aupdate_state().
_CLOSE_NODE = "_care_close"

ProfileResolver = Callable[[str], tuple[ModelEndpoint, ThinkingLevel | None]]


@dataclass(frozen=True)
class _ThreadEndpointSecrets:
    """In-memory-only API key cache for inline endpoints within the current process lifetime."""

    root_api_key: SecretStr | None = None
    node_api_keys: dict[str, SecretStr] = field(default_factory=dict)


def _persist_run_options(
    options: RunOptions,
    *,
    profile: str | None,
) -> PersistedRunOptions:
    """Strip all secrets from RunOptions into a checkpoint-safe PersistedRunOptions snapshot."""
    persisted_overrides: dict[str, PersistedNodeOverride] = {}
    for node_id, override in options.node_overrides.items():
        persisted_overrides[node_id] = PersistedNodeOverride(
            adapter=override.adapter,
            endpoint=(
                PersistedEndpoint.from_endpoint(override.endpoint)
                if override.endpoint is not None
                else None
            ),
            thinking_level=override.thinking_level,
            persist_session=override.persist_session,
            tool_policy=override.tool_policy,
            tools=override.tools,
        )
    return PersistedRunOptions(
        adapter=options.adapter,
        profile=profile,
        endpoint=(
            PersistedEndpoint.from_endpoint(options.endpoint)
            if options.endpoint is not None
            else None
        ),
        thinking_level=options.thinking_level,
        persist_session=options.persist_session,
        node_overrides=persisted_overrides,
    )


def _extract_secrets(options: RunOptions) -> _ThreadEndpointSecrets:
    """Extract inline API keys to keep strictly in memory (never written to checkpoints)."""
    root_key = options.endpoint.api_key if options.endpoint is not None else None
    node_keys = {
        node_id: override.endpoint.api_key
        for node_id, override in options.node_overrides.items()
        if override.endpoint is not None and override.endpoint.api_key is not None
    }
    return _ThreadEndpointSecrets(root_api_key=root_key, node_api_keys=node_keys)


def _derive_thread_title(prompt: str, *, max_chars: int = 120) -> str:
    """Derive a compact single-line thread title from the initial prompt."""
    for raw_line in prompt.splitlines():
        cleaned = " ".join(raw_line.split())
        if cleaned:
            if len(cleaned) > max_chars:
                return cleaned[: max_chars - 1].rstrip() + "…"
            return cleaned
    return "Untitled"


class WorkflowRunner:
    """Production execution engine for code-defined LangGraph Agent workflows."""

    def __init__(
        self,
        adapter_registry: AdapterRegistry,
        workflow_registry: WorkflowRegistry | None = None,
        *,
        persistence: PersistenceBackend | None = None,
        profile_resolver: ProfileResolver | None = None,
    ) -> None:
        self._adapters = adapter_registry
        self._workflow_registry = workflow_registry or WorkflowRegistry()
        self._persistence: PersistenceBackend = persistence or SqlitePersistenceBackend(":memory:")
        self._profile_resolver = profile_resolver
        self._live_runs = LiveRunRegistry()
        self._thread_secrets: dict[str, _ThreadEndpointSecrets] = {}
        self._compiled_cache: dict[str, Any] = {}
        self._entered = False

    async def __aenter__(self) -> WorkflowRunner:
        await self._persistence.__aenter__()
        self._entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._entered = False
        self._compiled_cache.clear()
        self._thread_secrets.clear()
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
        profile: str | None = None,
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
        workspace = Path(workspace_dir).expanduser()
        if not workspace.is_dir():
            raise InvalidInputError(
                f"workspace_dir does not exist or is not a directory: '{workspace_dir}'"
            )

        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"
        effective_thread_id = thread_id or effective_run_id

        effective_endpoint = endpoint
        effective_thinking = thinking_level
        effective_profile = profile.strip() if profile is not None and profile.strip() else None
        if effective_profile is not None and effective_endpoint is None:
            if self._profile_resolver is None:
                raise InvalidInputError(
                    "'profile' is only supported in local mode; pass 'endpoint' directly"
                )
            resolved_ep, profile_thinking = self._profile_resolver(effective_profile)
            effective_endpoint = resolved_ep
            if effective_thinking is None:
                effective_thinking = profile_thinking

        options = RunOptions(
            adapter=adapter,
            endpoint=effective_endpoint,
            thinking_level=effective_thinking,
            persist_session=persist_session,
            node_overrides=dict(node_overrides) if node_overrides else {},
        )
        persisted_options = _persist_run_options(options, profile=effective_profile)

        with self._live_runs.claim(
            effective_thread_id, effective_run_id, workflow_id=workflow_name
        ):
            try:
                inspected = await inspect_thread(
                    compiled,
                    inflight_store,
                    effective_thread_id,
                    expected_workflow_id=workflow_name,
                    cleanup=True,
                )
            except NotFoundError:
                inspected = None

            preserved_sessions: dict[str, str] = {}
            if inspected is not None and inspected.state.resumable:
                if inspected.state.status not in ("failed", "aborted"):
                    raise StateConflictError(
                        f"Thread '{effective_thread_id}' is {inspected.state.status}; "
                        "call resume() or discard()"
                    )
                if inspected.paused_sessions:
                    preserved_sessions = dict(inspected.paused_sessions)

            self._thread_secrets[effective_thread_id] = _extract_secrets(options)

            # BinaryOperatorAggregate skips the reducer on the very first write to an empty
            # channel, so pass {} / "running" for fresh threads and None to trigger reset on
            # existing threads.
            has_outputs = inspected is not None and "node_outputs" in inspected.snapshot.values
            has_status = inspected is not None and "status" in inspected.snapshot.values
            resolved_workspace = str(workspace.resolve())
            initial_state: dict[str, Any] = {
                "run_id": effective_run_id,
                "thread_id": effective_thread_id,
                "workflow_id": workflow_name,
                "prompt": prompt,
                "workspace_dir": resolved_workspace,
                "run_options": persisted_options,
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
                workspace_dir=resolved_workspace,
                thread_title=_derive_thread_title(prompt),
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
        adapter: str | None = None,
        profile: str | None = None,
        endpoint: ModelEndpoint | None = None,
        thinking_level: ThinkingLevel | None = None,
        persist_session: bool | None = None,
        node_overrides: Mapping[str, NodeRuntimeOverride] | None = None,
        interaction_handler: InteractionHandler | None = None,
        interaction_mode: InteractionMode = "cancel",
    ) -> AsyncIterator[RuntimeEvent]:
        """统一断点恢复入口：默认沿用暂停前执行配置，显式传参时覆盖并写回父图状态。"""
        compiled, workflow_name = self._get_compiled(workflow)
        inflight_store = self._require_inflight_store()
        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"

        with self._live_runs.claim(thread_id, effective_run_id, workflow_id=workflow_name):
            inspected = await inspect_thread(
                compiled,
                inflight_store,
                thread_id,
                expected_workflow_id=workflow_name,
                cleanup=True,
            )
            if not inspected.state.resumable:
                raise StateConflictError(
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

            options, persisted_options = self._resolve_resume_options(
                thread_id=thread_id,
                saved=inspected.run_options,
                adapter=adapter,
                profile=profile,
                endpoint=endpoint,
                thinking_level=thinking_level,
                persist_session=persist_session,
                node_overrides=node_overrides,
            )
            self._thread_secrets[thread_id] = _extract_secrets(options)

            options_changed = persisted_options != inspected.run_options
            updated_options = persisted_options if options_changed else None
            graph_input = plan.graph_input
            if updated_options is not None and isinstance(graph_input, Command):
                graph_input = Command(
                    update={"run_options": updated_options},
                    resume=graph_input.resume,
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
                workspace_dir=str(inspected.snapshot.values.get("workspace_dir", "")),
                thread_title=_derive_thread_title(str(inspected.snapshot.values.get("prompt", ""))),
                exec_ctx=exec_ctx,
                graph_input=graph_input,
                opening_event=WorkflowResumedEvent(resumed_from=plan.reason),
                interaction_handler=interaction_handler,
                interaction_mode=interaction_mode,
                staged_replies=plan.staged_replies,
                updated_run_options=updated_options,
            ):
                yield event

    async def discard(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        """放弃未完成的挂起/崩溃/失败轮次，保留全部分支的 session_ref 并收口为不可恢复终态。"""
        compiled, workflow_name = self._get_compiled(workflow)
        store = self._require_inflight_store()

        with self._live_runs.claim(
            thread_id, f"discard-{uuid4().hex[:12]}", workflow_id=workflow_name
        ):
            inspected = await inspect_thread(
                compiled,
                store,
                thread_id,
                expected_workflow_id=workflow_name,
                cleanup=True,
            )
            state = inspected.state
            if not state.resumable:
                self._thread_secrets.pop(thread_id, None)
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

            self._thread_secrets.pop(thread_id, None)
            closed = await inspect_thread(
                compiled,
                store,
                thread_id,
                expected_workflow_id=workflow_name,
                cleanup=True,
            )
            await self._require_history_store().update_thread_status(
                thread_id,
                status=closed.state.status,
            )
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
        compiled, workflow_name = self._get_compiled(workflow)
        snapshot = await compiled.aget_state({"configurable": {"thread_id": thread_id}})
        stored_workflow_id = snapshot.values.get("workflow_id")
        if stored_workflow_id is not None and stored_workflow_id != workflow_name:
            raise StateConflictError(
                f"Thread '{thread_id}' belongs to workflow '{stored_workflow_id}', "
                f"not '{workflow_name}'"
            )
        return dict(snapshot.values)

    async def get_thread_state(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        """纯只读查询线程状态：在线线程由 LiveRunRegistry 提供实时视图，离线线程走 WAL 对账。"""
        compiled, workflow_name = self._get_compiled(workflow)
        active = self._live_runs.get_active_run(thread_id)
        if active is not None:
            if active.workflow_id != workflow_name:
                raise StateConflictError(
                    f"Thread '{thread_id}' belongs to workflow '{active.workflow_id}', "
                    f"not '{workflow_name}'"
                )
            snapshot = await compiled.aget_state(
                {"configurable": {"thread_id": thread_id}}, subgraphs=True
            )
            outputs = dict(snapshot.values.get("node_outputs", {}))
            sessions = dict(snapshot.values.get("node_sessions", {}))
            live_interrupts = tuple(
                PendingInterrupt(
                    interrupt_id=None,
                    node_id=node_id,
                    kind="agent_interaction",
                    interaction=req,
                )
                for node_id, req in active.pending_interactions
            )
            return ThreadState(
                thread_id=thread_id,
                status="running",
                resumable=False,
                active_run_id=active.run_id,
                node_outputs=outputs,
                node_sessions=sessions,
                next_nodes=active.active_nodes or snapshot.next,
                interrupts=live_interrupts,
                inflight_nodes=(),
            )

        inspected = await inspect_thread(
            compiled,
            self._require_inflight_store(),
            thread_id,
            expected_workflow_id=workflow_name,
        )
        return inspected.state

    async def list_threads(
        self,
        *,
        workspace_dir: str | None = None,
        limit: int = 50,
    ) -> tuple[ThreadSummary, ...]:
        """List persisted threads ordered by recency, reconciled against live runs."""
        if limit < 1:
            raise InvalidInputError("'limit' must be at least 1")
        normalized_workspace: str | None = None
        if workspace_dir is not None:
            stripped = workspace_dir.strip()
            if not stripped:
                raise InvalidInputError("'workspace_dir' must be non-empty when provided")
            normalized_workspace = str(Path(stripped).expanduser().resolve())

        rows = await self._require_history_store().list_threads(
            workspace_dir=normalized_workspace,
            limit=limit,
        )
        reconciled: list[ThreadSummary] = []
        for row in rows:
            active = self._live_runs.get_active_run(row.thread_id)
            if active is not None:
                reconciled.append(
                    row.model_copy(
                        update={
                            "status": "running",
                            "active_run_id": active.run_id,
                            "last_run_id": active.run_id,
                        }
                    )
                )
            elif row.status == "running":
                reconciled.append(
                    row.model_copy(update={"status": "crashed", "active_run_id": None})
                )
            else:
                reconciled.append(row.model_copy(update={"active_run_id": None}))
        return tuple(reconciled)

    async def get_thread_events(self, thread_id: str) -> tuple[RuntimeEvent, ...]:
        """Return durable RuntimeEvents persisted across all runs of `thread_id`."""
        history_store = self._require_history_store()
        if await history_store.get_thread(thread_id) is None:
            raise NotFoundError(f"Unknown thread_id '{thread_id}'")
        return tuple(await history_store.list_events(thread_id))

    def _resolve_resume_options(
        self,
        *,
        thread_id: str,
        saved: PersistedRunOptions | None,
        adapter: str | None,
        profile: str | None,
        endpoint: ModelEndpoint | None,
        thinking_level: ThinkingLevel | None,
        persist_session: bool | None,
        node_overrides: Mapping[str, NodeRuntimeOverride] | None,
    ) -> tuple[RunOptions, PersistedRunOptions]:
        """Merge explicit resume overrides onto the thread's saved non-sensitive RunOptions."""
        base = saved if saved is not None else PersistedRunOptions()
        cached = self._thread_secrets.get(thread_id)
        clean_profile = profile.strip() if profile is not None and profile.strip() else None

        if clean_profile is not None:
            if endpoint is not None:
                effective_endpoint: ModelEndpoint | None = endpoint
                effective_thinking: ThinkingLevel | None = thinking_level
            else:
                if self._profile_resolver is None:
                    raise InvalidInputError(
                        "'profile' is only supported in local mode; pass 'endpoint' directly"
                    )
                resolved_ep, profile_thinking = self._profile_resolver(clean_profile)
                effective_endpoint = resolved_ep
                effective_thinking = (
                    thinking_level if thinking_level is not None else profile_thinking
                )
            effective_profile: str | None = clean_profile
        elif endpoint is not None:
            effective_endpoint = endpoint
            effective_profile = None
            effective_thinking = thinking_level
        else:
            effective_profile = base.profile
            if base.profile is not None:
                if self._profile_resolver is None:
                    raise InvalidInputError(
                        f"Thread '{thread_id}' uses local profile '{base.profile}' which cannot "
                        "be resolved in this mode; pass 'endpoint' directly"
                    )
                resolved_ep, profile_thinking = self._profile_resolver(base.profile)
                effective_endpoint = resolved_ep
                effective_thinking = (
                    thinking_level
                    if thinking_level is not None
                    else (
                        base.thinking_level
                        if base.thinking_level is not None
                        else profile_thinking
                    )
                )
            elif base.endpoint is not None:
                key = cached.root_api_key if cached is not None else None
                if base.endpoint.requires_api_key and key is None:
                    raise InvalidInputError(
                        f"Thread '{thread_id}' was started with an inline endpoint requiring "
                        "'api_key'; pass 'endpoint' to resume"
                    )
                effective_endpoint = base.endpoint.to_endpoint(api_key=key)
                effective_thinking = (
                    thinking_level if thinking_level is not None else base.thinking_level
                )
            else:
                effective_endpoint = None
                effective_thinking = (
                    thinking_level if thinking_level is not None else base.thinking_level
                )

        effective_adapter = adapter if adapter is not None else base.adapter
        effective_persist = persist_session if persist_session is not None else base.persist_session

        if node_overrides is not None:
            effective_overrides = dict(node_overrides)
        else:
            effective_overrides = {}
            for node_id, persisted_ov in base.node_overrides.items():
                restored_ep: ModelEndpoint | None = None
                if persisted_ov.endpoint is not None:
                    node_key = cached.node_api_keys.get(node_id) if cached is not None else None
                    if persisted_ov.endpoint.requires_api_key and node_key is None:
                        raise InvalidInputError(
                            f"Thread '{thread_id}' node '{node_id}' was started with an inline "
                            "endpoint requiring 'api_key'; pass 'node_overrides' to resume"
                        )
                    restored_ep = persisted_ov.endpoint.to_endpoint(api_key=node_key)
                effective_overrides[node_id] = NodeRuntimeOverride(
                    adapter=persisted_ov.adapter,
                    endpoint=restored_ep,
                    thinking_level=persisted_ov.thinking_level,
                    persist_session=persisted_ov.persist_session,
                    tool_policy=persisted_ov.tool_policy,
                    tools=persisted_ov.tools,
                )

        options = RunOptions(
            adapter=effective_adapter,
            endpoint=effective_endpoint,
            thinking_level=effective_thinking,
            persist_session=effective_persist,
            node_overrides=effective_overrides,
        )
        persisted = _persist_run_options(options, profile=effective_profile)
        return options, persisted

    async def _stream_execution(
        self,
        *,
        compiled: Any,
        workflow_name: str,
        workspace_dir: str,
        thread_title: str,
        exec_ctx: ExecutionContext,
        graph_input: Any,
        opening_event: WorkflowStartedEvent | WorkflowResumedEvent,
        interaction_handler: InteractionHandler | None,
        interaction_mode: InteractionMode,
        staged_replies: dict[str, Any] | None = None,
        updated_run_options: PersistedRunOptions | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        seq = 0
        pending_replies = dict(staged_replies) if staged_replies else {}
        history_store = self._require_history_store()

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

        async def _record(event: RuntimeEvent) -> RuntimeEvent:
            if not event.is_ephemeral:
                await history_store.append_event(event)
            return event

        self._live_runs.configure_run(
            exec_ctx.run_id,
            interaction_handler=interaction_handler,
            interaction_mode=interaction_mode,
        )

        try:
            await history_store.upsert_thread(
                exec_ctx.thread_id,
                workflow_id=workflow_name,
                workspace_dir=workspace_dir,
                title=thread_title,
                status="running",
                last_run_id=exec_ctx.run_id,
            )
            yield await _record(_wrap(opening_event))
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
                        yield await _record(_wrap(packet.event, node_id=packet.node_id))

                inspected = await inspect_thread(
                    compiled,
                    exec_ctx.inflight_store,
                    exec_ctx.thread_id,
                    expected_workflow_id=workflow_name,
                    cleanup=True,
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
                if updated_run_options is not None:
                    graph_input = Command(
                        update={"run_options": updated_run_options},
                        resume=resume_map,
                    )
                    updated_run_options = None
                else:
                    graph_input = Command(resume=resume_map)
                pending_replies = {}

            if state.status == "interrupted":
                event_node_id = state.interrupts[0].node_id if len(state.interrupts) == 1 else None
                interrupted_event = _wrap(
                    WorkflowInterruptedEvent(interrupts=state.interrupts),
                    node_id=event_node_id,
                )
                await history_store.append_event(interrupted_event)
                await history_store.update_thread_status(exec_ctx.thread_id, status="interrupted")
                yield interrupted_event
            elif state.status in ("failed", "aborted", "completed"):
                if not state.resumable:
                    self._thread_secrets.pop(exec_ctx.thread_id, None)
                first_failure = next(
                    (item.failure for item in state.interrupts if item.failure is not None),
                    snapshot.values.get("failure"),
                )
                completed_event = _wrap(
                    WorkflowCompletedEvent(
                        status=state.status,
                        final_output=snapshot.values.get("latest_output", ""),
                        failure=first_failure,
                    )
                )
                await history_store.append_event(completed_event)
                await history_store.update_thread_status(exec_ctx.thread_id, status=state.status)
                yield completed_event
            else:
                raise RuntimeError(f"Graph stopped with pending nodes {state.next_nodes}")
        except AdapterError as exc:
            await history_store.append_event(
                _wrap(RuntimeErrorEvent.from_raw(code="adapter_error", message=str(exc)))
            )
            raise
        except Exception:
            await history_store.append_event(
                _wrap(RuntimeErrorEvent(code="internal_error", message="Internal server error"))
            )
            raise
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

    def _require_history_store(self) -> ThreadHistoryStore:
        if not self._entered:
            raise RuntimeError(
                "WorkflowRunner must be used inside 'async with WorkflowRunner(...)'"
            )
        return self._persistence.history_store


def _compile_workflow(
    builder: WorkflowBuilder,
    checkpointer: BaseCheckpointSaver[Any],
) -> Any:
    graph = builder()
    if _CLOSE_NODE not in graph.nodes:
        graph.add_node(_CLOSE_NODE, lambda _state: {})
        graph.add_edge(_CLOSE_NODE, END)
    return graph.compile(checkpointer=checkpointer)
