"""Live in-memory run registry, runtime options, and LangGraph ExecutionContext."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.adapters.base import (
    AgentCapabilities,
    AgentSession,
    ModelEndpoint,
    ThinkingLevel,
    ToolPolicy,
)
from agent_runtime.adapters.errors import AdapterCapabilityError, AdapterError
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.errors import InvalidInputError, NotFoundError, StateConflictError
from agent_runtime.orchestrator.persistence import InflightNodeStore
from agent_runtime.protocol.events import InteractionRequestEvent, InteractionResponse

InteractionHandler = Callable[[InteractionRequestEvent], Awaitable[InteractionResponse]]
InteractionMode = Literal["cancel", "wait", "suspend"]

_ABORT_CANCEL_SETTLE_TIMEOUT_S = 2.0


class _SuspendSignal:
    """内部哨兵：由 runner.suspend() / suspend_all() 注入，通知等待中的热交互立即冷挂起。"""


SUSPEND_SIGNAL = _SuspendSignal()


class NodeRuntimeOverride(BaseModel):
    """Per-node runtime override supporting heterogeneous multi-agent configuration."""

    model_config = ConfigDict(frozen=True)

    adapter: str | None = None
    endpoint: ModelEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool | None = None
    tool_policy: ToolPolicy | None = None
    tools: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _validate_override(self) -> NodeRuntimeOverride:
        if self.tools is not None and self.tool_policy not in (None, "full"):
            raise ValueError("Cannot combine explicit 'tools' with non-default 'tool_policy'")
        return self


class RunOptions(BaseModel):
    """Per-run execution configuration injected via LangGraph runtime context."""

    model_config = ConfigDict(frozen=True)

    adapter: str = "pi"
    endpoint: ModelEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool = True
    node_overrides: dict[str, NodeRuntimeOverride] = Field(default_factory=dict)


@dataclass
class PendingInteraction:
    """记录某个活跃 run 当前正阻塞等待人工回复的交互请求及其同步信号。"""

    request: InteractionRequestEvent
    future: asyncio.Future[InteractionResponse | _SuspendSignal]
    settled: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _ActiveSessionEntry:
    session: AgentSession
    capabilities: AgentCapabilities


class LiveRunRegistry:
    """Encapsulates active run locks, live sessions, and hot interaction coordination."""

    def __init__(self) -> None:
        self._active_run_ids: dict[str, str] = {}
        self._active_sessions: dict[str, dict[str, _ActiveSessionEntry]] = {}
        self._interaction_handlers: dict[str, InteractionHandler] = {}
        self._interaction_modes: dict[str, InteractionMode] = {}
        self._pending_interactions: dict[str, dict[str, PendingInteraction]] = {}
        self._suspended_runs: set[str] = set()

    def is_thread_running(self, thread_id: str) -> bool:
        return thread_id in self._active_run_ids

    @contextlib.contextmanager
    def claim(self, thread_id: str, run_id: str) -> Iterator[None]:
        if thread_id in self._active_run_ids or run_id in self._active_run_ids.values():
            raise StateConflictError(f"Thread '{thread_id}' or run '{run_id}' is already running")
        self._active_run_ids[thread_id] = run_id
        try:
            yield
        finally:
            del self._active_run_ids[thread_id]

    def configure_run(
        self,
        run_id: str,
        *,
        interaction_handler: InteractionHandler | None,
        interaction_mode: InteractionMode,
    ) -> None:
        if interaction_handler is not None:
            self._interaction_handlers[run_id] = interaction_handler
        self._interaction_modes[run_id] = interaction_mode

    def cleanup_run(self, run_id: str) -> None:
        self._interaction_handlers.pop(run_id, None)
        self._interaction_modes.pop(run_id, None)
        self._pending_interactions.pop(run_id, None)
        self._suspended_runs.discard(run_id)

    def get_interaction_handler(self, run_id: str) -> InteractionHandler | None:
        return self._interaction_handlers.get(run_id)

    def get_interaction_mode(self, run_id: str) -> InteractionMode:
        return self._interaction_modes.get(run_id, "cancel")

    def is_suspended(self, run_id: str) -> bool:
        return run_id in self._suspended_runs

    def register_session(
        self,
        run_id: str,
        node_id: str,
        session: AgentSession,
        capabilities: AgentCapabilities,
    ) -> None:
        self._active_sessions.setdefault(run_id, {})[node_id] = _ActiveSessionEntry(
            session=session,
            capabilities=capabilities,
        )

    def unregister_session(self, run_id: str, node_id: str, session: AgentSession) -> None:
        sessions = self._active_sessions.get(run_id)
        if sessions is not None:
            entry = sessions.get(node_id)
            if entry is not None and entry.session is session:
                del sessions[node_id]
                if not sessions:
                    self._active_sessions.pop(run_id, None)
        self.unregister_pending(run_id, node_id)

    def register_pending(
        self,
        run_id: str,
        node_id: str,
        pending: PendingInteraction,
    ) -> None:
        self._pending_interactions.setdefault(run_id, {})[node_id] = pending

    def unregister_pending(self, run_id: str, node_id: str) -> None:
        pending_map = self._pending_interactions.get(run_id)
        if pending_map is not None:
            pending_map.pop(node_id, None)
            if not pending_map:
                self._pending_interactions.pop(run_id, None)

    async def respond_interaction(
        self,
        run_id: str,
        response: InteractionResponse,
        *,
        node_id: str | None = None,
    ) -> None:
        """向正在线热等待的节点回传用户审批结果或提问回答。"""
        pending_map = self._pending_interactions.get(run_id)
        if not pending_map:
            raise NotFoundError(f"No pending interaction found for run_id '{run_id}'")
        if node_id is not None:
            pending = pending_map.get(node_id)
            if pending is None:
                raise NotFoundError(
                    f"No pending interaction found for run_id '{run_id}', node_id '{node_id}'"
                )
            if pending.request.request_id != response.request_id:
                raise InvalidInputError(
                    f"Interaction request_id mismatch for run_id '{run_id}' node '{node_id}': "
                    f"expected '{pending.request.request_id}', got '{response.request_id}'"
                )
        else:
            matches = [
                (nid, item)
                for nid, item in pending_map.items()
                if item.request.request_id == response.request_id
            ]
            if len(matches) == 1:
                pending = matches[0][1]
            elif len(matches) > 1:
                nodes = ", ".join(nid for nid, _ in matches)
                raise InvalidInputError(
                    f"Ambiguous request_id '{response.request_id}' across nodes ({nodes}); "
                    "specify node_id"
                )
            else:
                expected = ", ".join(item.request.request_id for item in pending_map.values())
                raise InvalidInputError(
                    f"Interaction request_id mismatch for run_id '{run_id}': "
                    f"expected one of [{expected}], got '{response.request_id}'"
                )
        if not pending.future.done():
            pending.future.set_result(response)

    async def suspend(self, run_id: str) -> None:
        """优雅冷挂起指定活跃运行：落盘全部活跃分支的 session_ref 与待回复交互。"""
        self._suspended_runs.add(run_id)
        signaled_nodes: set[str] = set()
        pending_map = self._pending_interactions.get(run_id)
        if pending_map:
            for node_id, pending in list(pending_map.items()):
                if not pending.future.done():
                    pending.future.set_result(SUSPEND_SIGNAL)
                    signaled_nodes.add(node_id)

        sessions = self._active_sessions.get(run_id)
        if sessions:
            for node_id, entry in list(sessions.items()):
                if node_id not in signaled_nodes and entry.capabilities.abort:
                    with contextlib.suppress(AdapterError, OSError):
                        await entry.session.abort()

    async def suspend_all(self) -> None:
        """优雅冷挂起当前全部活跃运行（供桌面端 before-quit / SIGTERM 退出前调用）。"""
        active_ids = set(self._active_sessions) | set(self._pending_interactions)
        for run_id in list(active_ids):
            await self.suspend(run_id)

    async def steer(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        entry = self._require_active_session(run_id, node_id=node_id)
        if not entry.capabilities.steering:
            raise AdapterCapabilityError("Active adapter does not support mid-turn steering")
        await entry.session.steer(message)

    async def follow_up(self, run_id: str, message: str, *, node_id: str | None = None) -> None:
        entry = self._require_active_session(run_id, node_id=node_id)
        if not entry.capabilities.follow_up:
            raise AdapterCapabilityError("Active adapter does not support queued follow-up")
        await entry.session.follow_up(message)

    async def abort(self, run_id: str) -> None:
        sessions = self._active_sessions.get(run_id)
        if not sessions:
            raise NotFoundError(f"No active agent session found for run_id '{run_id}'")
        entries = list(sessions.values())
        for entry in entries:
            if not entry.capabilities.abort:
                raise AdapterCapabilityError("Active adapter does not support abort()")

        # 先以 cancel 唤醒全部等待中的热交互并等待响应发送完毕，避免 clear_queue/abort 抢跑
        pending_to_settle: list[PendingInteraction] = []
        pending_map = self._pending_interactions.get(run_id)
        if pending_map:
            for pending in list(pending_map.values()):
                if not pending.future.done():
                    pending.future.set_result(
                        InteractionResponse(
                            request_id=pending.request.request_id, decision="cancel"
                        )
                    )
                pending_to_settle.append(pending)

        if pending_to_settle:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_ABORT_CANCEL_SETTLE_TIMEOUT_S):
                    await asyncio.gather(*(item.settled.wait() for item in pending_to_settle))

        for entry in entries:
            await entry.session.abort()

    def _require_active_session(
        self,
        run_id: str,
        *,
        node_id: str | None = None,
    ) -> _ActiveSessionEntry:
        sessions = self._active_sessions.get(run_id)
        if not sessions:
            raise NotFoundError(f"No active agent session found for run_id '{run_id}'")
        if node_id is not None:
            entry = sessions.get(node_id)
            if entry is None:
                raise NotFoundError(
                    f"No active agent session found for run_id '{run_id}', node_id '{node_id}'"
                )
            return entry
        if len(sessions) == 1:
            return next(iter(sessions.values()))
        nodes = ", ".join(sorted(sessions))
        raise InvalidInputError(
            f"Multiple active agent sessions for run_id '{run_id}' ({nodes}); specify node_id"
        )


@dataclass(frozen=True)
class ExecutionContext:
    """Per-run execution dependencies injected into LangGraph nodes via `context=`."""

    run_id: str
    thread_id: str
    options: RunOptions
    adapters: AdapterRegistry
    live_runs: LiveRunRegistry
    inflight_store: InflightNodeStore
