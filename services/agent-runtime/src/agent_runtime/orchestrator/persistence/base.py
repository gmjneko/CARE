"""Storage-agnostic persistence protocols for LangGraph checkpoints and inflight WAL."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, ConfigDict

from agent_runtime.orchestrator.state import (
    NodeOutput,
    PersistedEndpoint,
    PersistedNodeOverride,
    PersistedRunOptions,
)
from agent_runtime.protocol.events import (
    AgentFailure,
    InteractionRequestEvent,
    RuntimeEvent,
    TokenUsage,
)

ThreadStatus = Literal["completed", "interrupted", "aborted", "failed", "crashed", "running"]

CHECKPOINT_SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=[
        (NodeOutput.__module__, NodeOutput.__qualname__),
        (PersistedEndpoint.__module__, PersistedEndpoint.__qualname__),
        (PersistedNodeOverride.__module__, PersistedNodeOverride.__qualname__),
        (PersistedRunOptions.__module__, PersistedRunOptions.__qualname__),
        (AgentFailure.__module__, AgentFailure.__qualname__),
        (InteractionRequestEvent.__module__, InteractionRequestEvent.__qualname__),
        (TokenUsage.__module__, TokenUsage.__qualname__),
    ]
)


class InflightNodeRecord(BaseModel):
    """一条尚未提交正式 Checkpoint 的活跃节点预写记录。"""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    node_id: str
    role: str
    task_id: str
    session_ref: str | None = None
    pending_interaction: InteractionRequestEvent | None = None
    updated_at: str


@runtime_checkable
class InflightNodeStore(Protocol):
    """Write-ahead store persisting active node session_ref and pending interaction."""

    async def upsert(
        self,
        thread_id: str,
        node_id: str,
        *,
        role: str,
        task_id: str,
        session_ref: str | None,
        pending_interaction: InteractionRequestEvent | None,
    ) -> None: ...

    async def delete(self, record: InflightNodeRecord) -> None: ...

    async def list_for_thread(self, thread_id: str) -> list[InflightNodeRecord]: ...


class ThreadSummary(BaseModel):
    """Persisted thread metadata for listing conversations and restoring client views."""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    workflow_id: str
    workspace_dir: str
    title: str
    status: ThreadStatus
    active_run_id: str | None = None
    last_run_id: str | None = None
    created_at: str
    updated_at: str


@runtime_checkable
class ThreadHistoryStore(Protocol):
    """Durable store for thread metadata and non-ephemeral RuntimeEvent timelines."""

    async def upsert_thread(
        self,
        thread_id: str,
        *,
        workflow_id: str,
        workspace_dir: str,
        title: str,
        status: ThreadStatus,
        last_run_id: str | None,
    ) -> None: ...

    async def update_thread_status(self, thread_id: str, *, status: ThreadStatus) -> None: ...

    async def append_event(self, event: RuntimeEvent) -> None: ...

    async def get_thread(self, thread_id: str) -> ThreadSummary | None: ...

    async def list_threads(
        self,
        *,
        workspace_dir: str | None = None,
        limit: int = 50,
    ) -> list[ThreadSummary]: ...

    async def list_events(self, thread_id: str) -> list[RuntimeEvent]: ...


@runtime_checkable
class PersistenceBackend(Protocol):
    """Async resource managing LangGraph's checkpointer, inflight WAL, and thread history."""

    async def __aenter__(self) -> PersistenceBackend: ...

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any]: ...

    @property
    def inflight_store(self) -> InflightNodeStore: ...

    @property
    def history_store(self) -> ThreadHistoryStore: ...


__all__ = [
    "CHECKPOINT_SERDE",
    "InflightNodeRecord",
    "InflightNodeStore",
    "PersistenceBackend",
    "ThreadHistoryStore",
    "ThreadStatus",
    "ThreadSummary",
]
