"""Thin protocol contracts for pluggable coding agent backends."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from agent_runtime.protocol.events import AgentEvent

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class AgentCapabilities(BaseModel):
    """Declarative capability matrix queried by the orchestrator."""

    model_config = ConfigDict(frozen=True)

    streaming: bool = True
    steering: bool = False
    follow_up: bool = False
    abort: bool = False
    session_resume: bool = False
    interactions: bool = False


class SessionConfig(BaseModel):
    """Backend-agnostic configuration for opening an isolated agent session."""

    model_config = ConfigDict(frozen=True)

    cwd: Path
    model: str | None = None
    provider: str | None = None
    thinking_level: ThinkingLevel | None = None
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    tools: tuple[str, ...] | None = None
    no_tools: bool = False
    extensions: tuple[Path, ...] = ()
    session_ref: str | None = None
    persist_session: bool = False

    @model_validator(mode="after")
    def _validate_combinations(self) -> SessionConfig:
        if self.provider and not self.model:
            raise ValueError("'provider' requires 'model' to be specified")
        if self.no_tools and self.tools is not None:
            raise ValueError("Cannot set both 'no_tools=True' and explicit 'tools'")
        if self.session_ref and not self.persist_session:
            raise ValueError("'session_ref' requires 'persist_session=True'")
        return self


class InteractionResponse(BaseModel):
    """Host decision/input answering a blocking InteractionRequestEvent."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    decision: Literal["approve", "deny", "cancel"] = "cancel"
    feedback: str | None = None
    value: str | None = None


@runtime_checkable
class AgentSession(Protocol):
    """Stateful, single-agent session lifecycle."""

    @property
    def session_id(self) -> str | None: ...

    @property
    def session_ref(self) -> str | None:
        """Durable reference suitable for resuming this session, if persisted."""
        ...

    def run_turn(self, prompt: str) -> AsyncIterator[AgentEvent]: ...

    async def steer(self, message: str) -> None: ...

    async def follow_up(self, message: str) -> None: ...

    async def abort(self) -> None: ...

    async def respond_interaction(self, response: InteractionResponse) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class AgentAdapter(Protocol):
    """Factory for opening sessions against a specific agent backend."""

    kind: str
    capabilities: AgentCapabilities

    def open_session(self, config: SessionConfig) -> AbstractAsyncContextManager[AgentSession]: ...
