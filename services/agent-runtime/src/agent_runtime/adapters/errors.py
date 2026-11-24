"""Structured exception hierarchy for AgentAdapter implementations."""

from __future__ import annotations

from agent_runtime.protocol.events import redact_sensitive_text


class AdapterError(Exception):
    """Base error for all adapter failures."""


class AdapterStartupError(AdapterError):
    """Raised when an agent backend fails to start or complete its initial handshake."""


class AdapterTransportError(AdapterError):
    """Raised when the underlying process/transport dies, times out, or breaks."""


class AdapterProtocolError(AdapterError):
    """Raised when inbound wire data violates the expected backend protocol."""


class AdapterRpcError(AdapterError):
    """Raised when a backend explicitly rejects a control RPC command."""

    def __init__(self, command: str, detail: str) -> None:
        self.command = command
        self.detail = redact_sensitive_text(detail, max_chars=512)
        super().__init__(f"RPC command '{command}' failed: {self.detail}")
