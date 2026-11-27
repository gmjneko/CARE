"""Explicit domain exceptions shared across settings, registries, orchestrator, runtime, and API.

Inheriting from built-in `KeyError` / `ValueError` keeps Python callers and tests ergonomic while
allowing the HTTP boundary to map only intentional domain errors to 4xx status codes (so accidental
internal `KeyError` / `ValueError` bugs surface as 500 Internal Server Error instead of 404/400).
"""

from __future__ import annotations


class NotFoundError(KeyError):
    """Raised when a requested workflow, adapter, profile, run, or thread does not exist."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class InvalidInputError(ValueError):
    """Raised when caller-supplied input violates domain rules or preconditions."""


class StateConflictError(ValueError):
    """Raised when an operation conflicts with the current run or thread lifecycle state."""


__all__ = [
    "InvalidInputError",
    "NotFoundError",
    "StateConflictError",
]
