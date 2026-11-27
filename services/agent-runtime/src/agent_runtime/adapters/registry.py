"""Explicit registry mapping adapter kind names to AgentAdapter instances."""

from __future__ import annotations

from agent_runtime.adapters.base import AgentAdapter
from agent_runtime.errors import NotFoundError


class AdapterRegistry:
    """Explicit dependency container for configured AgentAdapter instances."""

    def __init__(self, adapters: dict[str, AgentAdapter] | None = None) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        if adapters:
            for kind, adapter in adapters.items():
                self.register(adapter, kind=kind)

    def register(self, adapter: AgentAdapter, *, kind: str | None = None) -> None:
        key = kind or adapter.kind
        if not key:
            raise ValueError("Adapter kind must be a non-empty string")
        if key in self._adapters:
            raise ValueError(f"Adapter '{key}' is already registered")
        self._adapters[key] = adapter

    def get(self, kind: str) -> AgentAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            available = ", ".join(sorted(self._adapters)) or "<none>"
            raise NotFoundError(
                f"Unknown agent adapter '{kind}'. Registered adapters: {available}"
            ) from exc

    def list_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
