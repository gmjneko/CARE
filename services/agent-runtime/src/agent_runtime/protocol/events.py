"""Canonical event, interaction, usage, and failure models for CARE Agent Runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from agent_runtime.protocol.redaction import redact_sensitive_text

EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "text.delta",
        "thinking.delta",
        "tool.output_delta",
    }
)


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


class InteractionResponse(BaseModel):
    """Host decision/input answering a blocking InteractionRequestEvent."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    decision: Literal["approve", "deny", "cancel"] = "cancel"
    feedback: str | None = None
    value: str | None = None


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
    prompt: str


class WorkflowResumedEvent(BaseModel):
    """工作流从历史断点恢复执行时发射的起始事件。"""

    model_config = ConfigDict(frozen=True)

    type: Literal["workflow.resumed"] = "workflow.resumed"
    resumed_from: Literal[
        "workflow_interrupt", "agent_interaction", "awaiting_input", "failure", "crash"
    ]


class NodeStartedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["node.started"] = "node.started"
    node_id: str
    role: str
    adapter: str | None = None


class NodeCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["node.completed"] = "node.completed"
    node_id: str
    role: str
    status: Literal["completed", "interrupted", "failed", "aborted"]
    summary: str = ""
    usage: TokenUsage | None = None
    failure: AgentFailure | None = None


PendingInterruptKind = Literal[
    "workflow_interrupt",
    "agent_interaction",
    "awaiting_input",
    "suspended",
    "failed",
    "aborted",
    "crashed",
]


class PendingInterrupt(BaseModel):
    """一个等待外部决策或恢复的挂起分支（Agent 节点内暂停、Workflow interrupt 或崩溃任务）。"""

    model_config = ConfigDict(frozen=True)

    interrupt_id: str | None = None
    node_id: str
    kind: PendingInterruptKind
    interaction: InteractionRequestEvent | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    failure: AgentFailure | None = None


class WorkflowInterruptedEvent(BaseModel):
    """工作流因节点间 interrupt() 或节点内待回复交互/挂起而暂停时发射的终态事件。"""

    model_config = ConfigDict(frozen=True)

    type: Literal["workflow.interrupted"] = "workflow.interrupted"
    interrupts: tuple[PendingInterrupt, ...] = ()


class WorkflowCompletedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["workflow.completed"] = "workflow.completed"
    status: Literal["completed", "failed", "aborted"]
    final_output: str = ""
    failure: AgentFailure | None = None


class RuntimeErrorEvent(BaseModel):
    """Terminal runtime/transport error frame when a run stream terminates with an exception."""

    model_config = ConfigDict(frozen=True)

    type: Literal["runtime.error"] = "runtime.error"
    code: Literal["adapter_error", "internal_error"]
    message: str

    @classmethod
    def from_raw(
        cls,
        *,
        code: Literal["adapter_error", "internal_error"],
        message: str,
    ) -> RuntimeErrorEvent:
        return cls(
            code=code,
            message=redact_sensitive_text(message, max_chars=512),
        )


WorkflowEvent = Annotated[
    WorkflowStartedEvent
    | WorkflowResumedEvent
    | NodeStartedEvent
    | NodeCompletedEvent
    | WorkflowInterruptedEvent
    | WorkflowCompletedEvent,
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
    | WorkflowResumedEvent
    | NodeStartedEvent
    | NodeCompletedEvent
    | WorkflowInterruptedEvent
    | WorkflowCompletedEvent
    | RuntimeErrorEvent,
    Field(discriminator="type"),
]


class RuntimeEvent(BaseModel):
    """Sequenced canonical event envelope emitted by WorkflowRunner."""

    model_config = ConfigDict(frozen=True)

    seq: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str
    thread_id: str
    workflow_id: str
    node_id: str | None = None
    payload: RuntimeEventPayload

    @computed_field
    @property
    def is_ephemeral(self) -> bool:
        return self.payload.type in EPHEMERAL_EVENT_TYPES
