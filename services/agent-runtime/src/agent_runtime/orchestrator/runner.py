"""WorkflowRunner: compiles LangGraph workflows with SQLite checkpoints and event streams."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent_runtime.adapters.base import ThinkingLevel
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.orchestrator.node import InteractionHandler, RunContext, StreamPacket
from agent_runtime.orchestrator.registry import WorkflowBuilder, WorkflowRegistry
from agent_runtime.orchestrator.state import WorkflowState
from agent_runtime.protocol.events import (
    AgentFailure,
    RuntimeEvent,
    RuntimeEventPayload,
    WorkflowCompletedEvent,
    WorkflowStartedEvent,
)


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

    async def __aenter__(self) -> WorkflowRunner:
        stack = AsyncExitStack()
        try:
            checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(self._checkpoint_db)
            )
            await checkpointer.setup()
        except BaseException:
            await stack.aclose()
            raise
        self._exit_stack = stack
        self._checkpointer = checkpointer
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._checkpointer = None

    async def run(
        self,
        workflow: str | WorkflowBuilder,
        prompt: str,
        workspace_dir: str | Path,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        adapter: str = "pi",
        model: str | None = None,
        provider: str | None = None,
        thinking_level: ThinkingLevel | None = None,
        interaction_handler: InteractionHandler | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        checkpointer = self._require_checkpointer()
        workflow_name, builder = self._resolve_workflow(workflow)
        compiled = builder(
            self._run_context,
            adapter=adapter,
            model=model,
            provider=provider,
            thinking_level=thinking_level,
        ).compile(checkpointer=checkpointer)

        effective_run_id = run_id or f"run-{uuid4().hex[:12]}"
        effective_thread_id = thread_id or effective_run_id

        seq = 0

        def _wrap(payload: RuntimeEventPayload, *, node_id: str | None = None) -> RuntimeEvent:
            nonlocal seq
            seq += 1
            return RuntimeEvent(
                seq=seq,
                run_id=effective_run_id,
                workflow_id=workflow_name,
                node_id=node_id,
                payload=payload,
            )

        if interaction_handler is not None:
            self._run_context.interaction_handlers[effective_run_id] = interaction_handler

        initial_state: WorkflowState = {
            "run_id": effective_run_id,
            "workflow_id": workflow_name,
            "prompt": prompt,
            "workspace_dir": str(Path(workspace_dir).resolve()),
            "node_outputs": {},
            "latest_output": "",
            "status": "running",
            "failure": None,
        }

        last_status: Literal["running", "completed", "failed", "aborted"] = "running"
        last_output = ""
        last_failure: AgentFailure | None = None

        try:
            yield _wrap(WorkflowStartedEvent(preset=workflow_name, prompt=prompt))

            async for chunk in compiled.astream(
                initial_state,
                config={"configurable": {"thread_id": effective_thread_id}},
                stream_mode=["custom", "updates"],
                version="v2",
            ):
                chunk_dict = cast(dict[str, Any], chunk)
                chunk_type = chunk_dict.get("type")
                data = chunk_dict.get("data")

                if chunk_type == "custom" and isinstance(data, StreamPacket):
                    yield _wrap(data.event, node_id=data.node_id)
                elif chunk_type == "updates" and isinstance(data, dict):
                    for node_update in data.values():
                        if not isinstance(node_update, dict):
                            continue
                        if "status" in node_update:
                            last_status = node_update["status"]
                        if "latest_output" in node_update:
                            last_output = node_update["latest_output"]
                        if "failure" in node_update:
                            last_failure = node_update["failure"]

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
            self._run_context.interaction_handlers.pop(effective_run_id, None)

    async def steer(self, run_id: str, message: str) -> None:
        session = self._require_active_session(run_id)
        await session.steer(message)

    async def follow_up(self, run_id: str, message: str) -> None:
        session = self._require_active_session(run_id)
        await session.follow_up(message)

    async def abort(self, run_id: str) -> None:
        session = self._require_active_session(run_id)
        await session.abort()

    async def get_checkpoint_state(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> dict[str, Any]:
        checkpointer = self._require_checkpointer()
        _, builder = self._resolve_workflow(workflow)
        compiled = builder(self._run_context).compile(checkpointer=checkpointer)
        snapshot = await compiled.aget_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values)

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

    def _require_active_session(self, run_id: str):
        try:
            return self._run_context.active_sessions[run_id]
        except KeyError as exc:
            raise KeyError(f"No active agent session found for run_id '{run_id}'") from exc
