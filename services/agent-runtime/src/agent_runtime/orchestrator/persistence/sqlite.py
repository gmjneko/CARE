"""SQLite persistence backend for local mode (`AsyncSqliteSaver` + `care_inflight_nodes` WAL)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent_runtime.orchestrator.persistence.base import CHECKPOINT_SERDE, InflightNodeRecord
from agent_runtime.protocol.events import InteractionRequestEvent

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS care_inflight_nodes (
    thread_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    role TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_ref TEXT,
    pending_interaction_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, node_id)
)
"""

_EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "thread_id",
        "node_id",
        "role",
        "task_id",
        "session_ref",
        "pending_interaction_json",
        "updated_at",
    }
)


class SqliteInflightNodeStore:
    """基于 `checkpoints.sqlite` 同库存储的轻量预写日志（WAL）实现。"""

    def __init__(self, conn: Any, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    async def setup(self) -> None:
        """初始化预写表并校验 schema（不兼容时启动阶段立即报错，不静默删表）。"""
        async with self._lock:
            await self._conn.execute(_CREATE_TABLE_SQL)
            async with self._conn.execute("PRAGMA table_info(care_inflight_nodes)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if columns != _EXPECTED_COLUMNS:
                missing = sorted(_EXPECTED_COLUMNS - columns)
                extra = sorted(columns - _EXPECTED_COLUMNS)
                raise RuntimeError(
                    "Incompatible 'care_inflight_nodes' table schema "
                    f"(missing={missing}, extra={extra}); reset local state with "
                    "`uv run python scripts/reset_local_state.py` or remove checkpoints.sqlite"
                )
            await self._conn.commit()

    async def upsert(
        self,
        thread_id: str,
        node_id: str,
        *,
        role: str,
        task_id: str,
        session_ref: str | None,
        pending_interaction: InteractionRequestEvent | None,
    ) -> None:
        """写入或刷新活跃节点的 session_ref 与当前阻塞交互请求。"""
        interaction_json = (
            pending_interaction.model_dump_json() if pending_interaction is not None else None
        )
        now = datetime.now(UTC).isoformat()
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO care_inflight_nodes
                    (thread_id, node_id, role, task_id, session_ref,
                     pending_interaction_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, node_id) DO UPDATE SET
                    role = excluded.role,
                    task_id = excluded.task_id,
                    session_ref = excluded.session_ref,
                    pending_interaction_json = excluded.pending_interaction_json,
                    updated_at = excluded.updated_at
                """,
                (thread_id, node_id, role, task_id, session_ref, interaction_json, now),
            )
            await self._conn.commit()

    async def delete(self, record: InflightNodeRecord) -> None:
        """在节点正式提交 Checkpoint 后清理对应的预写记录。"""
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM care_inflight_nodes WHERE thread_id = ? AND node_id = ? "
                "AND task_id = ?",
                (record.thread_id, record.node_id, record.task_id),
            )
            await self._conn.commit()

    async def list_for_thread(self, thread_id: str) -> list[InflightNodeRecord]:
        """读取指定 thread_id 残留的所有预写记录（正常提交后应为空）。"""
        async with self._lock:
            cursor = await self._conn.execute(
                """
                SELECT thread_id, node_id, role, task_id, session_ref,
                       pending_interaction_json, updated_at
                FROM care_inflight_nodes
                WHERE thread_id = ?
                ORDER BY updated_at ASC
                """,
                (thread_id,),
            )
            rows = await cursor.fetchall()

        records: list[InflightNodeRecord] = []
        for (
            row_thread,
            row_node,
            row_role,
            task_id,
            row_session,
            row_interaction_json,
            row_updated,
        ) in rows:
            interaction = (
                InteractionRequestEvent.model_validate_json(row_interaction_json)
                if row_interaction_json
                else None
            )
            records.append(
                InflightNodeRecord(
                    thread_id=row_thread,
                    node_id=row_node,
                    role=row_role,
                    task_id=task_id,
                    session_ref=row_session,
                    pending_interaction=interaction,
                    updated_at=row_updated,
                )
            )
        return records


class SqlitePersistenceBackend:
    """SQLite-backed PersistenceBackend sharing one connection across checkpoints and WAL."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._exit_stack: AsyncExitStack | None = None
        self._checkpointer: AsyncSqliteSaver | None = None
        self._inflight_store: SqliteInflightNodeStore | None = None

    @property
    def checkpointer(self) -> BaseCheckpointSaver[Any]:
        if self._checkpointer is None:
            raise RuntimeError("SqlitePersistenceBackend must be entered before accessing storage")
        return self._checkpointer

    @property
    def inflight_store(self) -> SqliteInflightNodeStore:
        if self._inflight_store is None:
            raise RuntimeError("SqlitePersistenceBackend must be entered before accessing storage")
        return self._inflight_store

    async def __aenter__(self) -> SqlitePersistenceBackend:
        if self._db_path != ":memory:":
            expanded = Path(self._db_path).expanduser()
            expanded.parent.mkdir(parents=True, exist_ok=True)
            conn_str = str(expanded)
        else:
            conn_str = ":memory:"

        stack = AsyncExitStack()
        try:
            raw_saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(conn_str))
            checkpointer = AsyncSqliteSaver(raw_saver.conn, serde=CHECKPOINT_SERDE)
            await checkpointer.setup()
            inflight_store = SqliteInflightNodeStore(checkpointer.conn, checkpointer.lock)
            await inflight_store.setup()
        except BaseException:
            await stack.aclose()
            raise

        self._exit_stack = stack
        self._checkpointer = checkpointer
        self._inflight_store = inflight_store
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._checkpointer = None
        self._inflight_store = None


__all__ = [
    "SqliteInflightNodeStore",
    "SqlitePersistenceBackend",
]
