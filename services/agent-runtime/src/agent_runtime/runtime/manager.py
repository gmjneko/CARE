"""Application-layer RunManager decoupling background workflow execution from stream consumers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agent_runtime.adapters.base import ModelEndpoint, ThinkingLevel
from agent_runtime.orchestrator import (
    InteractionHandler,
    InteractionMode,
    NodeRuntimeOverride,
    ResumeInput,
    ThreadState,
    WorkflowBuilder,
    WorkflowRunner,
)
from agent_runtime.protocol import (
    InteractionResponse,
    RuntimeEvent,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
    redact_sensitive_text,
)

RunStatus = Literal["running", "completed", "interrupted", "failed", "aborted"]

_SHUTDOWN_DRAIN_TIMEOUT_S = 5.0


class RunSnapshot(BaseModel):
    """Point-in-time summary of a managed workflow run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    thread_id: str
    workflow_id: str
    status: RunStatus
    last_seq: int
    error: str | None = None


class _ManagedRun:
    """Internal background task + append-only event buffer supporting multi-subscriber fan-out."""

    def __init__(self, first_event: RuntimeEvent) -> None:
        self.run_id = first_event.run_id
        self.thread_id = first_event.thread_id
        self.workflow_id = first_event.workflow_id
        self.status: RunStatus = "running"
        self.events: list[RuntimeEvent] = [first_event]
        self.exception: BaseException | None = None
        self.done: bool = False
        self.condition = asyncio.Condition()
        self.task: asyncio.Task[None] | None = None

    def snapshot(self) -> RunSnapshot:
        return RunSnapshot(
            run_id=self.run_id,
            thread_id=self.thread_id,
            workflow_id=self.workflow_id,
            status=self.status,
            last_seq=self.events[-1].seq if self.events else 0,
            error=(
                redact_sensitive_text(str(self.exception), max_chars=512)
                if self.exception is not None
                else None
            ),
        )

    async def pump(self, stream: AsyncIterator[RuntimeEvent]) -> None:
        try:
            async for event in stream:
                async with self.condition:
                    self.events.append(event)
                    if isinstance(event.payload, WorkflowInterruptedEvent):
                        self.status = "interrupted"
                    elif isinstance(event.payload, WorkflowCompletedEvent):
                        self.status = event.payload.status
                    self.condition.notify_all()
        except BaseException as exc:
            async with self.condition:
                self.exception = exc
                self.status = "failed"
                self.done = True
                self.condition.notify_all()
            return
        finally:
            if hasattr(stream, "aclose"):
                with contextlib.suppress(BaseException):
                    await stream.aclose()

        async with self.condition:
            self.done = True
            self.condition.notify_all()


class RunManager:
    """Manages decoupled background workflow runs with replayable multi-subscriber event streams.

    Clients can start a run, disconnect mid-flight without cancelling execution, reconnect with
    `after_seq` to catch up on missed events, or attach multiple concurrent observers.
    """

    def __init__(self, runner: WorkflowRunner) -> None:
        self._runner = runner
        self._runs: dict[str, _ManagedRun] = {}
        self._entered = False

    async def __aenter__(self) -> RunManager:
        await self._runner.__aenter__()
        self._entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._entered = False
        active_tasks = [
            managed.task
            for managed in self._runs.values()
            if managed.task is not None and not managed.task.done()
        ]
        if active_tasks:
            with contextlib.suppress(BaseException):
                await self._runner.suspend_all()
            try:
                async with asyncio.timeout(_SHUTDOWN_DRAIN_TIMEOUT_S):
                    await asyncio.gather(*active_tasks, return_exceptions=True)
            except TimeoutError:
                for task in active_tasks:
                    if not task.done():
                        task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*active_tasks, return_exceptions=True)
        await self._runner.__aexit__(exc_type, exc, tb)

    async def start_run(
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
    ) -> RunSnapshot:
        """Start a workflow run in a background task, failing fast on invalid inputs or state."""
        self._require_entered()
        stream = self._runner.run(
            workflow,
            prompt,
            workspace_dir,
            run_id=run_id,
            thread_id=thread_id,
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
            persist_session=persist_session,
            node_overrides=node_overrides,
            interaction_handler=interaction_handler,
            interaction_mode=interaction_mode,
        )
        return await self._launch(stream)

    async def start_resume(
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
    ) -> RunSnapshot:
        """Resume a paused/failed/crashed thread in a background task, failing fast on bad input."""
        self._require_entered()
        stream = self._runner.resume(
            workflow,
            thread_id,
            run_id=run_id,
            node_id=node_id,
            resume_value=resume_value,
            interaction_response=interaction_response,
            prompt=prompt,
            complete=complete,
            resumes=resumes,
            adapter=adapter,
            endpoint=endpoint,
            thinking_level=thinking_level,
            persist_session=persist_session,
            node_overrides=node_overrides,
            interaction_handler=interaction_handler,
            interaction_mode=interaction_mode,
        )
        return await self._launch(stream)

    async def subscribe(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[RuntimeEvent]:
        """Replay events with `seq > after_seq` and stream live updates until the run finishes."""
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        managed = self._require_run(run_id)
        cursor = after_seq

        while True:
            async with managed.condition:
                while cursor >= len(managed.events) and not managed.done:
                    await managed.condition.wait()
                batch = managed.events[cursor:]
                cursor += len(batch)
                done = managed.done
                exc = managed.exception

            for event in batch:
                yield event

            if done:
                if exc is not None:
                    raise exc
                return

    async def wait(self, run_id: str) -> RunSnapshot:
        """Await background completion of `run_id`, re-raising any unhandled run exception."""
        managed = self._require_run(run_id)
        if managed.task is not None:
            await managed.task
        if managed.exception is not None:
            raise managed.exception
        return managed.snapshot()

    def get_run(self, run_id: str) -> RunSnapshot:
        return self._require_run(run_id).snapshot()

    def list_runs(self) -> tuple[RunSnapshot, ...]:
        return tuple(managed.snapshot() for managed in self._runs.values())

    async def respond_interaction(
        self,
        run_id: str,
        response: InteractionResponse,
        *,
        node_id: str | None = None,
    ) -> None:
        await self._runner.respond_interaction(run_id, response, node_id=node_id)

    async def steer(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        await self._runner.steer(run_id, message, node_id=node_id)

    async def follow_up(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        await self._runner.follow_up(run_id, message, node_id=node_id)

    async def abort(self, run_id: str) -> None:
        await self._runner.abort(run_id)

    async def suspend(self, run_id: str) -> None:
        await self._runner.suspend(run_id)

    async def suspend_all(self) -> None:
        await self._runner.suspend_all()

    async def discard(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        return await self._runner.discard(workflow, thread_id)

    async def get_thread_state(
        self,
        workflow: str | WorkflowBuilder,
        thread_id: str,
    ) -> ThreadState:
        return await self._runner.get_thread_state(workflow, thread_id)

    async def _launch(self, stream: AsyncIterator[RuntimeEvent]) -> RunSnapshot:
        try:
            # Advance to the opening WorkflowStarted/WorkflowResumed event so workflow resolution,
            # thread-claim locking, and resume-input validation fail fast before returning.
            first_event = await anext(stream)
        except BaseException:
            if hasattr(stream, "aclose"):
                with contextlib.suppress(BaseException):
                    await stream.aclose()
            raise

        if first_event.run_id in self._runs:
            if hasattr(stream, "aclose"):
                with contextlib.suppress(BaseException):
                    await stream.aclose()
            raise ValueError(f"Run '{first_event.run_id}' already exists in RunManager")

        managed = _ManagedRun(first_event)
        self._runs[managed.run_id] = managed
        managed.task = asyncio.create_task(
            managed.pump(stream),
            name=f"care-run-{managed.run_id}",
        )
        return managed.snapshot()

    def _require_entered(self) -> None:
        if not self._entered:
            raise RuntimeError("RunManager must be used inside 'async with RunManager(...)'")

    def _require_run(self, run_id: str) -> _ManagedRun:
        managed = self._runs.get(run_id)
        if managed is None:
            raise KeyError(f"Unknown run_id '{run_id}'")
        return managed


__all__ = ["RunManager", "RunSnapshot", "RunStatus"]
