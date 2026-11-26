"""PiRpcAdapter and PiRpcSession implementing the AgentAdapter protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_runtime.adapters.base import AgentCapabilities, SessionConfig
from agent_runtime.adapters.errors import (
    AdapterError,
    AdapterProtocolError,
    AdapterRpcError,
    AdapterStartupError,
)
from agent_runtime.adapters.pi.normalizer import PiEventNormalizer
from agent_runtime.adapters.pi.transport import PiRpcConfig, PiRpcTransport, TransportClosed
from agent_runtime.protocol.events import (
    AgentEvent,
    AgentFailure,
    InteractionRequestEvent,
    InteractionResponse,
    TurnCompletedEvent,
    TurnStartedEvent,
)

PI_CAPABILITIES = AgentCapabilities(
    streaming=True,
    steering=True,
    follow_up=True,
    abort=True,
    session_resume=True,
    interactions=True,
)


class PiRpcSession:
    """Stateful session connected to a single `pi --mode rpc` subprocess."""

    def __init__(self, transport: PiRpcTransport, initial_state: dict[str, Any]) -> None:
        self._transport = transport
        self._session_id: str | None = _opt_str(initial_state.get("sessionId"))
        self._session_ref: str | None = _opt_str(initial_state.get("sessionFile"))
        self._is_compacting: bool = bool(initial_state.get("isCompacting", False))
        self._turn_seq = 0
        self._turn_active = False
        self._abort_requested = False
        self._pending_interactions: dict[str, str] = {}

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def session_ref(self) -> str | None:
        return self._session_ref

    async def run_turn(self, prompt: str) -> AsyncIterator[AgentEvent]:
        if self._turn_active:
            raise RuntimeError("Concurrent run_turn() on the same PiRpcSession is not allowed")

        self._turn_active = True
        self._abort_requested = False
        self._turn_seq += 1
        turn_id = f"turn-{self._turn_seq}"
        normalizer = PiEventNormalizer()
        normalizer.is_compacting = self._is_compacting

        try:
            yield TurnStartedEvent(turn_id=turn_id, session_id=self._session_id)

            # Hold prompt submission while Pi is compacting so it isn't rejected.
            if self._is_compacting:
                async for event in self._drain_until_compaction_ends(normalizer):
                    yield event

            prompt_id = self._transport.next_id("prompt")
            compaction_retried = False
            await self._transport.send({"id": prompt_id, "type": "prompt", "message": prompt})

            while True:
                item = await self._transport.inbound_queue.get()

                if isinstance(item, TransportClosed):
                    if isinstance(item.error, AdapterProtocolError):
                        raise item.error
                    if self._abort_requested:
                        yield TurnCompletedEvent(
                            turn_id=turn_id,
                            status="aborted",
                            final_text=normalizer.final_text,
                            usage=normalizer.latest_usage,
                            session_reusable=False,
                        )
                        return
                    failure = AgentFailure.from_raw(
                        failure_class="transport_error",
                        message=str(item.error) if item.error else "Pi RPC stream closed early",
                        retryable=False,
                    )
                    yield TurnCompletedEvent(
                        turn_id=turn_id,
                        status="failed",
                        final_text=normalizer.final_text,
                        usage=normalizer.latest_usage,
                        failure=failure,
                        session_reusable=False,
                    )
                    return

                record_type = item.get("type")

                if record_type == "response":
                    resp_id = item.get("id")
                    if resp_id == prompt_id:
                        if not item.get("success", False):
                            raw_err = str(item.get("error") or "Prompt rejected by Pi")
                            if "compaction" in raw_err.lower() and not compaction_retried:
                                compaction_retried = True
                                self._is_compacting = True
                                normalizer.is_compacting = True
                                async for event in self._drain_until_compaction_ends(normalizer):
                                    yield event
                                prompt_id = self._transport.next_id("prompt")
                                await self._transport.send(
                                    {"id": prompt_id, "type": "prompt", "message": prompt}
                                )
                                continue

                            failure = AgentFailure.from_raw(
                                failure_class="provider_error",
                                message=raw_err,
                                retryable=False,
                            )
                            yield TurnCompletedEvent(
                                turn_id=turn_id,
                                status="failed",
                                final_text=normalizer.final_text,
                                usage=normalizer.latest_usage,
                                failure=failure,
                                session_reusable=True,
                            )
                            return

                        data = item.get("data")
                        if (
                            isinstance(data, dict)
                            and data.get("disposition") == "handled"
                            and await self._is_idle()
                        ):
                            yield TurnCompletedEvent(
                                turn_id=turn_id,
                                status="completed",
                                final_text=normalizer.final_text,
                                usage=normalizer.latest_usage,
                                session_reusable=True,
                            )
                            return
                        continue

                    # Non-prompt fire-and-forget commands (steer/followUp) surface rejection fast.
                    if not item.get("success", False):
                        cmd_name = str(item.get("command") or "unknown")
                        raise AdapterRpcError(cmd_name, str(item.get("error") or "RPC failed"))
                    continue

                if record_type == "agent_settled":
                    aborted = bool(item.get("aborted", False)) or self._abort_requested
                    if not aborted and not await self._is_idle():
                        # Post-turn hook or queued follow-up resumed work; wait for next settle.
                        continue

                    if aborted:
                        status = "aborted"
                    elif normalizer.failure is not None:
                        status = "failed"
                    else:
                        status = "completed"

                    yield TurnCompletedEvent(
                        turn_id=turn_id,
                        status=status,
                        final_text=normalizer.final_text,
                        usage=normalizer.latest_usage,
                        failure=normalizer.failure if status == "failed" else None,
                        session_reusable=True,
                    )
                    return

                for event in normalizer.consume(item):
                    if isinstance(event, InteractionRequestEvent) and event.blocking:
                        self._pending_interactions[event.request_id] = event.kind
                    yield event
                self._is_compacting = normalizer.is_compacting
        finally:
            self._pending_interactions.clear()
            self._turn_active = False

    async def steer(self, message: str) -> None:
        # Use ID-correlated request() so RPC rejections surface directly to the steer() caller
        # without terminating the concurrent run_turn() event stream.
        await self._transport.request(
            {
                "id": self._transport.next_id("steer"),
                "type": "prompt",
                "message": message,
                "streamingBehavior": "steer",
            }
        )

    async def follow_up(self, message: str) -> None:
        await self._transport.request(
            {
                "id": self._transport.next_id("follow-up"),
                "type": "prompt",
                "message": message,
                "streamingBehavior": "followUp",
            }
        )

    async def abort(self) -> None:
        self._abort_requested = True
        # Clear queued follow-ups before aborting so Pi does not immediately start queued work.
        await self._transport.request({"type": "clear_queue"})
        await self._transport.request({"type": "abort"})

    async def respond_interaction(self, response: InteractionResponse) -> None:
        kind = self._pending_interactions.pop(response.request_id, None)
        if kind is None:
            raise KeyError(f"No pending Pi interaction request '{response.request_id}'")

        payload: dict[str, Any] = {
            "type": "extension_ui_response",
            "id": response.request_id,
        }
        if response.decision == "cancel":
            payload["cancelled"] = True
        elif kind == "approval":
            payload["confirmed"] = response.decision == "approve"
        elif response.decision == "deny":
            payload["cancelled"] = True
        elif response.value is not None:
            payload["value"] = response.value
        else:
            raise ValueError(
                "InteractionResponse with decision='approve' for user_input requires 'value'"
            )
        await self._transport.send(payload)

    async def wait_closed(self) -> None:
        """等待底层 Pi 子进程关闭或意外退出。"""
        await self._transport.wait_closed()

    async def close(self) -> None:
        await self._transport.close()

    async def _drain_until_compaction_ends(
        self, normalizer: PiEventNormalizer
    ) -> AsyncIterator[AgentEvent]:
        while self._is_compacting:
            item = await self._transport.inbound_queue.get()
            if isinstance(item, TransportClosed):
                raise item.error or AdapterStartupError("Pi closed during compaction")
            for event in normalizer.consume(item):
                yield event
            self._is_compacting = normalizer.is_compacting

    async def _is_idle(self) -> bool:
        state = await self._transport.request({"type": "get_state"})
        self._session_id = _opt_str(state.get("sessionId")) or self._session_id
        self._session_ref = _opt_str(state.get("sessionFile")) or self._session_ref
        self._is_compacting = bool(state.get("isCompacting", False))
        is_streaming = bool(state.get("isStreaming", False))
        pending_count = int(state.get("pendingMessageCount") or 0)
        return not is_streaming and not self._is_compacting and pending_count == 0


class PiRpcAdapter:
    """AgentAdapter implementation backed by `pi --mode rpc` subprocesses."""

    kind: str = "pi"
    capabilities: AgentCapabilities = PI_CAPABILITIES

    def __init__(self, config: PiRpcConfig | None = None) -> None:
        self._config = config or PiRpcConfig()

    @asynccontextmanager
    async def open_session(self, config: SessionConfig) -> AsyncIterator[PiRpcSession]:
        transport = await PiRpcTransport.spawn(rpc_config=self._config, session_config=config)
        try:
            try:
                initial_state = await transport.request({"type": "get_state"})
            except AdapterError as exc:
                raise AdapterStartupError(f"Pi RPC startup handshake failed: {exc}") from exc
            session = PiRpcSession(transport, initial_state)
            yield session
        finally:
            await transport.close()


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
