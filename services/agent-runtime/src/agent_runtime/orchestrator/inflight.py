"""SQLite Write-Ahead 预写表：在节点运行或等待热交互期间持久化 session_ref 与待回复弹窗。

复用 LangGraph `AsyncSqliteSaver` 已打开的连接与并发锁（零额外连接、支持 `:memory:` 与磁盘文件），
避免节点尚未返回提交 Checkpoint 时遭遇 `kill -9` / 断电导致会话指针或交互上下文丢失。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_runtime.protocol.events import InteractionRequestEvent

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS care_inflight_nodes (
    thread_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    task_id TEXT,
    session_ref TEXT,
    pending_interaction_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, node_id)
)
"""


class InflightNodeRecord(BaseModel):
    """一条尚未提交正式 Checkpoint 的活跃节点预写记录。"""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    node_id: str
    task_id: str | None
    session_ref: str | None = None
    pending_interaction: InteractionRequestEvent | None = None
    updated_at: str


class InflightNodeStore:
    """基于 `checkpoints.sqlite` 同库存储的轻量预写日志（WAL）操作封装。"""

    def __init__(self, conn: Any, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    async def setup(self) -> None:
        """初始化预写表（幂等）。"""
        async with self._lock:
            await self._conn.execute(_CREATE_TABLE_SQL)
            async with self._conn.execute("PRAGMA table_info(care_inflight_nodes)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if "task_id" not in columns:
                await self._conn.execute("ALTER TABLE care_inflight_nodes ADD COLUMN task_id TEXT")
            await self._conn.commit()

    async def upsert(
        self,
        thread_id: str,
        node_id: str,
        *,
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
                    (thread_id, node_id, task_id, session_ref, pending_interaction_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, node_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    session_ref = excluded.session_ref,
                    pending_interaction_json = excluded.pending_interaction_json,
                    updated_at = excluded.updated_at
                """,
                (thread_id, node_id, task_id, session_ref, interaction_json, now),
            )
            await self._conn.commit()

    async def delete(self, record: InflightNodeRecord) -> None:
        """在节点正式提交 Checkpoint 后清理对应的预写记录。"""
        async with self._lock:
            await self._conn.execute(
                "DELETE FROM care_inflight_nodes WHERE thread_id = ? AND node_id = ? "
                "AND task_id IS ?",
                (record.thread_id, record.node_id, record.task_id),
            )
            await self._conn.commit()

    async def list_for_thread(self, thread_id: str) -> list[InflightNodeRecord]:
        """读取指定 thread_id 残留的所有预写记录（正常提交后应为空）。"""
        async with self._lock:
            cursor = await self._conn.execute(
                """
                SELECT thread_id, node_id, task_id, session_ref,
                       pending_interaction_json, updated_at
                FROM care_inflight_nodes
                WHERE thread_id = ?
                ORDER BY updated_at ASC
                """,
                (thread_id,),
            )
            rows = await cursor.fetchall()

        records: list[InflightNodeRecord] = []
        for row_thread, row_node, task_id, row_session, row_interaction_json, row_updated in rows:
            interaction = (
                InteractionRequestEvent.model_validate_json(row_interaction_json)
                if row_interaction_json
                else None
            )
            records.append(
                InflightNodeRecord(
                    thread_id=row_thread,
                    node_id=row_node,
                    task_id=task_id,
                    session_ref=row_session,
                    pending_interaction=interaction,
                    updated_at=row_updated,
                )
            )
        return records
