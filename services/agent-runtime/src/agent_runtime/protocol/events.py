"""Canonical event, usage, and failure models for CARE Agent Runtime."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "text.delta",
        "thinking.delta",
        "tool.output_delta",
    }
)

_MAX_FAILURE_MESSAGE_CHARS = 4096

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization header / bearer / basic tokens
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    # URL userinfo credentials (https://user:pass@host)
    (re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@"), r"\1[REDACTED]:[REDACTED]@"),
    # Common API key prefixes (OpenAI / Anthropic / DeepSeek / generic sk-...)
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}\b"), "[REDACTED_KEY]"),
    # JSON-style secret fields
    (
        re.compile(
            r'(?i)("(?:api[_-]?key|token|access[_-]?token|secret|password|authorization)"\s*:\s*")([^"]+)(")'
        ),
        r"\1[REDACTED]\3",
    ),
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact_sensitive_text(text: str, *, max_chars: int = _MAX_FAILURE_MESSAGE_CHARS) -> str:
    """Strip credentials, control characters, and excessive length from diagnostic text."""
    sanitized = text
    for pattern, replacement in _REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    sanitized = _CONTROL_CHARS_RE.sub("", sanitized).strip()
    if len(sanitized) > max_chars:
        return sanitized[: max_chars - 3] + "..."
    return sanitized


class TokenUsage(BaseModel):
    """Normalized token accounting reported by an agent turn or message."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0


FailureClass = Literal[
    "provider_error",
    "transport_error",
    "permission_error",
    "protocol_error",
    "usage_limit",
    "unknown",
]


class AgentFailure(BaseModel):
    """Structured, redacted failure metadata for routing and diagnostics."""

    model_config = ConfigDict(frozen=True)

    failure_class: FailureClass
    message: str
    code: str | None = None
    retryable: bool = False

    @classmethod
    def from_raw(
        cls,
        *,
        failure_class: FailureClass,
        message: str,
        code: str | None = None,
        retryable: bool = False,
    ) -> AgentFailure:
        return cls(
            failure_class=failure_class,
            message=redact_sensitive_text(message),
            code=code,
            retryable=retryable,
        )


# ---------------------------------------------------------------------------
# Atomic Agent Events (emitted by any AgentAdapter)
# ---------------------------------------------------------------------------


class TurnStartedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["turn.started"] = "turn.started"
    turn_id: str
    session_id: str | None = None


class TextDeltaEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["text.delta"] = "text.delta"
    delta: str
    content_index: int = 0


class ThinkingDeltaEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["thinking.delta"] = "thinking.delta"
    delta: str
    content_index: int = 0


class MessageCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["message.completed"] = "message.completed"
    role: Literal["user", "assistant"]
    text: str
    thinking: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None


class ToolStartedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["tool.started"] = "tool.started"
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    parent_tool_call_id: str | None = None


class ToolOutputDeltaEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["tool.output_delta"] = "tool.output_delta"
    tool_call_id: str
    tool_name: str
    chunk: str


class ToolCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["tool.completed"] = "tool.completed"
    tool_call_id: str
    tool_name: str
    output: str
    is_error: bool = False
    duration_ms: int | None = None


class CompactionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["compaction"] = "compaction"
    phase: Literal["started", "completed"]
    reason: str | None = None
    summary: str | None = None


class RetryEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["retry"] = "retry"
    phase: Literal["started", "completed"]
    attempt: int = 1
    max_attempts: int | None = None
    error: str | None = None
    succeeded: bool | None = None


class InteractionRequestEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["interaction.request"] = "interaction.request"
    request_id: str
    kind: Literal["approval", "user_input", "notify"]
    blocking: bool = True
    title: str | None = None
    message: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    input_mode: Literal["select", "text"] | None = None
    options: tuple[str, ...] | None = None


class UsageUpdatedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["usage.updated"] = "usage.updated"
    usage: TokenUsage


class TurnCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["turn.completed"] = "turn.completed"
    turn_id: str
    status: Literal["completed", "aborted", "failed"]
    final_text: str = ""
    usage: TokenUsage | None = None
    failure: AgentFailure | None = None
    session_reusable: bool = True


AgentEvent = Annotated[
    TurnStartedEvent
    | TextDeltaEvent
    | ThinkingDeltaEvent
    | MessageCompletedEvent
    | ToolStartedEvent
    | ToolOutputDeltaEvent
    | ToolCompletedEvent
    | CompactionEvent
    | RetryEvent
    | InteractionRequestEvent
    | UsageUpdatedEvent
    | TurnCompletedEvent,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Workflow-Level Events (emitted by LangGraph orchestrator)
# ---------------------------------------------------------------------------


class WorkflowStartedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["workflow.started"] = "workflow.started"
    preset: str
    goal: str


class NodeStartedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["node.started"] = "node.started"
    node_id: str
    role: str
    adapter: str


class NodeCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["node.completed"] = "node.completed"
    node_id: str
    role: str
    status: Literal["completed", "failed", "aborted"]
    summary: str = ""
    usage: TokenUsage | None = None
    failure: AgentFailure | None = None


class WorkflowCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["workflow.completed"] = "workflow.completed"
    status: Literal["completed", "failed", "aborted"]
    final_output: str = ""
    failure: AgentFailure | None = None


WorkflowEvent = Annotated[
    WorkflowStartedEvent | NodeStartedEvent | NodeCompletedEvent | WorkflowCompletedEvent,
    Field(discriminator="type"),
]

RuntimeEventPayload = Annotated[
    TurnStartedEvent
    | TextDeltaEvent
    | ThinkingDeltaEvent
    | MessageCompletedEvent
    | ToolStartedEvent
    | ToolOutputDeltaEvent
    | ToolCompletedEvent
    | CompactionEvent
    | RetryEvent
    | InteractionRequestEvent
    | UsageUpdatedEvent
    | TurnCompletedEvent
    | WorkflowStartedEvent
    | NodeStartedEvent
    | NodeCompletedEvent
    | WorkflowCompletedEvent,
    Field(discriminator="type"),
]


class RuntimeEvent(BaseModel):
    """Sequenced canonical event envelope emitted by WorkflowRunner."""

    model_config = ConfigDict(frozen=True)

    seq: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str
    workflow_id: str
    node_id: str | None = None
    payload: RuntimeEventPayload

    @property
    def is_ephemeral(self) -> bool:
        return self.payload.type in EPHEMERAL_EVENT_TYPES
