"""HTTP request and response Pydantic schemas reusing core runtime domain models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.adapters import AgentCapabilities, ApiProtocol, ModelEndpoint, ThinkingLevel
from agent_runtime.config import RuntimeMode
from agent_runtime.orchestrator import InteractionMode, NodeRuntimeOverride, ResumeInput
from agent_runtime.protocol import InteractionResponse
from agent_runtime.settings import LocalSettings, ModelProfile


class ErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: ErrorDetail


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    mode: RuntimeMode
    version: str


class WorkflowInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str


class AdapterInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    capabilities: AgentCapabilities


class StartRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow_id: str
    prompt: str
    workspace_dir: str
    run_id: str | None = None
    thread_id: str | None = None
    adapter: str = "pi"
    profile: str | None = None
    endpoint: ModelEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool = True
    node_overrides: dict[str, NodeRuntimeOverride] = Field(default_factory=dict)
    interaction_mode: InteractionMode = "wait"

    @model_validator(mode="after")
    def _validate_request(self) -> StartRunRequest:
        workflow_id = self.workflow_id.strip()
        if not workflow_id:
            raise ValueError("'workflow_id' must be non-empty")
        prompt = self.prompt.strip()
        if not prompt:
            raise ValueError("'prompt' must be non-empty")
        workspace_dir = self.workspace_dir.strip()
        if not workspace_dir:
            raise ValueError("'workspace_dir' must be non-empty")
        if self.profile is not None and self.endpoint is not None:
            raise ValueError("Cannot specify both 'profile' and 'endpoint'")
        object.__setattr__(self, "workflow_id", workflow_id)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "workspace_dir", workspace_dir)
        return self


class ResumeThreadRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str | None = None
    node_id: str | None = None
    resume_value: Any = None
    interaction_response: InteractionResponse | None = None
    prompt: str | None = None
    complete: bool = False
    resumes: dict[str, ResumeInput] | None = None
    adapter: str | None = None
    profile: str | None = None
    endpoint: ModelEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool | None = None
    node_overrides: dict[str, NodeRuntimeOverride] | None = None
    interaction_mode: InteractionMode = "wait"

    @model_validator(mode="after")
    def _validate_request(self) -> ResumeThreadRequest:
        if self.profile is not None and self.endpoint is not None:
            raise ValueError("Cannot specify both 'profile' and 'endpoint'")
        return self


class RespondInteractionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    response: InteractionResponse
    node_id: str | None = None


class MessageControlRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str
    node_id: str | None = None

    @model_validator(mode="after")
    def _validate_message(self) -> MessageControlRequest:
        message = self.message.strip()
        if not message:
            raise ValueError("'message' must be non-empty")
        object.__setattr__(self, "message", message)
        return self


class ModelProfileView(BaseModel):
    """Redacted local model profile representation (never exposes plaintext api_key)."""

    model_config = ConfigDict(frozen=True)

    model: str
    provider: str | None = None
    base_url: str | None = None
    api_protocol: ApiProtocol = "openai-completions"
    thinking_level: ThinkingLevel | None = None
    has_api_key: bool = False

    @classmethod
    def from_profile(cls, profile: ModelProfile) -> ModelProfileView:
        return cls(
            model=profile.model,
            provider=profile.provider,
            base_url=profile.base_url,
            api_protocol=profile.api_protocol,
            thinking_level=profile.thinking_level,
            has_api_key=profile.api_key is not None,
        )


class LocalSettingsResponse(BaseModel):
    """Redacted local `~/.care/settings.json` view returned by `GET/PUT /settings`."""

    model_config = ConfigDict(frozen=True)

    active_profile: str | None = None
    profiles: dict[str, ModelProfileView] = Field(default_factory=dict)
    adapter_bins: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: LocalSettings) -> LocalSettingsResponse:
        return cls(
            active_profile=settings.active_profile,
            profiles={
                name: ModelProfileView.from_profile(profile)
                for name, profile in settings.profiles.items()
            },
            adapter_bins=dict(settings.adapter_bins),
        )


__all__ = [
    "AdapterInfo",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "LocalSettingsResponse",
    "MessageControlRequest",
    "ModelProfileView",
    "RespondInteractionRequest",
    "ResumeThreadRequest",
    "StartRunRequest",
    "WorkflowInfo",
]
