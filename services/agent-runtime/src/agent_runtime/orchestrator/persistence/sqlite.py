"""SQLite persistence backend for local mode (`AsyncSqliteSaver` + `care_inflight_nodes` WAL)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent_runtime.orchestrator.persistence.base import (
    CHECKPOINT_SERDE,
    InflightNodeRecord,
    ThreadStatus,
    ThreadSummary,
)
from agent_runtime.protocol.events import InteractionRequestEvent, RuntimeEvent

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

_CREATE_THREADS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS care_threads (
    thread_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workspace_dir TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    last_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_EXPECTED_THREADS_COLUMNS: frozenset[str] = frozenset(
    {
        "thread_id",
        "workflow_id",
        "workspace_dir",
        "title",
        "status",
        "last_run_id",
        "created_at",
        "updated_at",
    }
)

_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS care_thread_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_EVENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_care_thread_events_thread_id_id
    ON care_thread_events (thread_id, id)
"""

_EXPECTED_EVENTS_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "thread_id",
        "run_id",
        "seq",
        "event_type",
        "event_json",
        "created_at",
    }
)


async def _verify_table_columns(
    conn: Any,
    table_name: str,
    expected: frozenset[str],
) -> None:
    async with conn.execute(f"PRAGMA table_info({table_name})") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if columns != expected:
        missing = sorted(expected - columns)
        extra = sorted(columns - expected)
        raise RuntimeError(
            f"Incompatible '{table_name}' table schema "
            f"(missing={missing}, extra={extra}); reset local state with "
            "`uv run python scripts/reset_local_state.py` or remove checkpoints.sqlite"
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
            await _verify_table_columns(self._conn, "care_inflight_nodes", _EXPECTED_COLUMNS)
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


class SqliteThreadHistoryStore:
    """SQLite-backed store for thread summaries and durable RuntimeEvent timelines."""

    def __init__(self, conn: Any, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    async def setup(self) -> None:
        async with self._lock:
            await self._conn.execute(_CREATE_THREADS_TABLE_SQL)
            await _verify_table_columns(self._conn, "care_threads", _EXPECTED_THREADS_COLUMNS)
            await self._conn.execute(_CREATE_EVENTS_TABLE_SQL)
            await _verify_table_columns(self._conn, "care_thread_events", _EXPECTED_EVENTS_COLUMNS)
            await self._conn.execute(_CREATE_EVENTS_INDEX_SQL)
            await self._conn.commit()

    async def upsert_thread(
        self,
        thread_id: str,
        *,
        workflow_id: str,
        workspace_dir: str,
        title: str,
        status: ThreadStatus,
        last_run_id: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO care_threads
                    (thread_id, workflow_id, workspace_dir, title, status,
                     last_run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    workspace_dir = CASE
                        WHEN excluded.workspace_dir != '' THEN excluded.workspace_dir
                        ELSE care_threads.workspace_dir
                    END,
                    status = excluded.status,
                    last_run_id = COALESCE(excluded.last_run_id, care_threads.last_run_id),
                    updated_at = excluded.updated_at
                """,
                (thread_id, workflow_id, workspace_dir, title, status, last_run_id, now, now),
            )
            await self._conn.commit()

    async def update_thread_status(self, thread_id: str, *, status: ThreadStatus) -> None:
        now = datetime.now(UTC).isoformat()
        async with self._lock:
            await self._conn.execute(
                "UPDATE care_threads SET status = ?, updated_at = ? WHERE thread_id = ?",
                (status, now, thread_id),
            )
            await self._conn.commit()

    async def append_event(self, event: RuntimeEvent) -> None:
        now = datetime.now(UTC).isoformat()
        payload_json = event.model_dump_json()
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO care_thread_events
                    (thread_id, run_id, seq, event_type, event_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.thread_id,
                    event.run_id,
                    event.seq,
                    event.payload.type,
                    payload_json,
                    now,
                ),
            )
            await self._conn.execute(
                "UPDATE care_threads SET updated_at = ? WHERE thread_id = ?",
                (now, event.thread_id),
            )
            await self._conn.commit()

    async def get_thread(self, thread_id: str) -> ThreadSummary | None:
        async with self._lock:
            cursor = await self._conn.execute(
                """
                SELECT thread_id, workflow_id, workspace_dir, title, status,
                       last_run_id, created_at, updated_at
                FROM care_threads
                WHERE thread_id = ?
                """,
                (thread_id,),
            )
            row = await cursor.fetchone()
        return _row_to_thread_summary(row) if row is not None else None

    async def list_threads(
        self,
        *,
        workspace_dir: str | None = None,
        limit: int = 50,
    ) -> list[ThreadSummary]:
        async with self._lock:
            if workspace_dir is not None:
                cursor = await self._conn.execute(
                    """
                    SELECT thread_id, workflow_id, workspace_dir, title, status,
                           last_run_id, created_at, updated_at
                    FROM care_threads
                    WHERE workspace_dir = ?
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (workspace_dir, limit),
                )
            else:
                cursor = await self._conn.execute(
                    """
                    SELECT thread_id, workflow_id, workspace_dir, title, status,
                           last_run_id, created_at, updated_at
                    FROM care_threads
                    ORDER BY updated_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = await cursor.fetchall()
        return [_row_to_thread_summary(row) for row in rows]

    async def list_events(self, thread_id: str) -> list[RuntimeEvent]:
        async with self._lock:
            cursor = await self._conn.execute(
                """
                SELECT event_json
                FROM care_thread_events
                WHERE thread_id = ?
                ORDER BY id ASC
                """,
                (thread_id,),
            )
            rows = await cursor.fetchall()
        return [RuntimeEvent.model_validate_json(row[0]) for row in rows]


def _row_to_thread_summary(row: tuple[Any, ...]) -> ThreadSummary:
    (
        thread_id,
        workflow_id,
        workspace_dir,
        title,
        status,
        last_run_id,
        created_at,
        updated_at,
    ) = row
    return ThreadSummary(
        thread_id=thread_id,
        workflow_id=workflow_id,
        workspace_dir=workspace_dir,
        title=title,
        status=status,
        last_run_id=last_run_id,
        created_at=created_at,
        updated_at=updated_at,
    )


class SqlitePersistenceBackend:
    """SQLite-backed PersistenceBackend sharing one connection across checkpoints, WAL, history."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._exit_stack: AsyncExitStack | None = None
        self._checkpointer: AsyncSqliteSaver | None = None
        self._inflight_store: SqliteInflightNodeStore | None = None
        self._history_store: SqliteThreadHistoryStore | None = None

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

    @property
    def history_store(self) -> SqliteThreadHistoryStore:
        if self._history_store is None:
            raise RuntimeError("SqlitePersistenceBackend must be entered before accessing storage")
        return self._history_store

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
            history_store = SqliteThreadHistoryStore(checkpointer.conn, checkpointer.lock)
            await history_store.setup()
        except BaseException:
            await stack.aclose()
            raise

        self._exit_stack = stack
        self._checkpointer = checkpointer
        self._inflight_store = inflight_store
        self._history_store = history_store
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._exit_stack = None
        self._checkpointer = None
        self._inflight_store = None
        self._history_store = None


__all__ = [
    "SqliteInflightNodeStore",
    "SqlitePersistenceBackend",
    "SqliteThreadHistoryStore",
]
