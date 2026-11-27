"""HTTP request and response Pydantic schemas reusing core runtime domain models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.adapters import AgentCapabilities, ModelEndpoint, ThinkingLevel
from agent_runtime.config import RuntimeMode
from agent_runtime.orchestrator import InteractionMode, NodeRuntimeOverride, ResumeInput
from agent_runtime.protocol import InteractionResponse


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
        if not self.workflow_id.strip():
            raise ValueError("'workflow_id' must be non-empty")
        if not self.prompt.strip():
            raise ValueError("'prompt' must be non-empty")
        if not self.workspace_dir.strip():
            raise ValueError("'workspace_dir' must be non-empty")
        if self.profile is not None and self.endpoint is not None:
            raise ValueError("Cannot specify both 'profile' and 'endpoint'")
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
        if not self.message.strip():
            raise ValueError("'message' must be non-empty")
        return self


__all__ = [
    "AdapterInfo",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "MessageControlRequest",
    "RespondInteractionRequest",
    "ResumeThreadRequest",
    "StartRunRequest",
    "WorkflowInfo",
]
