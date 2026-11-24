"""Thin protocol contracts for pluggable coding agent backends."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from agent_runtime.protocol.events import AgentEvent

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ApiProtocol = Literal[
    "openai-completions",
    "openai-responses",
    "anthropic-messages",
    "google-generative-ai",
]


class ModelEndpoint(BaseModel):
    """后端无关的模型连接端点（统一承接本地 settings.json 与云端 AI Gateway 下发的参数）。"""

    model_config = ConfigDict(frozen=True)

    provider: str | None = None
    model: str
    base_url: str | None = None
    api_protocol: ApiProtocol = "openai-completions"
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _validate_endpoint(self) -> ModelEndpoint:
        model = self.model.strip()
        if not model:
            raise ValueError("'model' must be a non-empty string")
        object.__setattr__(self, "model", model)

        if self.provider is not None:
            provider = self.provider.strip()
            if not provider:
                raise ValueError("'provider' must be non-empty when provided")
            object.__setattr__(self, "provider", provider)

        if self.base_url is not None:
            base_url = self.base_url.strip()
            if not base_url.startswith(("http://", "https://")):
                raise ValueError("'base_url' must start with 'http://' or 'https://'")
            object.__setattr__(self, "base_url", base_url)

        if self.api_key is not None:
            secret = self.api_key.get_secret_value().strip()
            if not secret:
                raise ValueError("'api_key' must be non-empty when provided")
            object.__setattr__(self, "api_key", SecretStr(secret))

        if self.api_key is not None and self.base_url is None and not self.provider:
            raise ValueError(
                "'api_key' without 'base_url' requires 'provider' to identify "
                "which built-in provider should receive the key"
            )
        return self


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
    endpoint: ModelEndpoint | None = None
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
