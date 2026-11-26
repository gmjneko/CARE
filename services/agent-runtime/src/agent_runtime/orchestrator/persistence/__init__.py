"""Persistence backend abstractions and built-in SQLite implementation."""

from agent_runtime.orchestrator.persistence.base import (
    CHECKPOINT_SERDE,
    InflightNodeRecord,
    InflightNodeStore,
    PersistenceBackend,
)
from agent_runtime.orchestrator.persistence.sqlite import (
    SqliteInflightNodeStore,
    SqlitePersistenceBackend,
)

__all__ = [
    "CHECKPOINT_SERDE",
    "InflightNodeRecord",
    "InflightNodeStore",
    "PersistenceBackend",
    "SqliteInflightNodeStore",
    "SqlitePersistenceBackend",
]
