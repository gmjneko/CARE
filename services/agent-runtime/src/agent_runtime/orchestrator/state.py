"""Compact LangGraph workflow state avoiding cross-node context bloat."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent_runtime.adapters.base import ApiProtocol, ModelEndpoint, ThinkingLevel, ToolPolicy
from agent_runtime.protocol.events import AgentFailure, InteractionRequestEvent, TokenUsage

WorkflowStatus = Literal["running", "interrupted", "completed", "failed", "aborted"]

_STATUS_PRIORITY: dict[WorkflowStatus, int] = {
    "running": 0,
    "completed": 1,
    "interrupted": 2,
    "aborted": 3,
    "failed": 4,
}


class PersistedEndpoint(BaseModel):
    """Non-sensitive ModelEndpoint metadata safe to persist in LangGraph checkpoints.

    Structurally omits `api_key` so secrets can never enter SQLite/Postgres checkpoints.
    """

    model_config = ConfigDict(frozen=True)

    model: str
    provider: str | None = None
    base_url: str | None = None
    api_protocol: ApiProtocol = "openai-completions"
    requires_api_key: bool = False

    @classmethod
    def from_endpoint(cls, endpoint: ModelEndpoint) -> PersistedEndpoint:
        return cls(
            model=endpoint.model,
            provider=endpoint.provider,
            base_url=endpoint.base_url,
            api_protocol=endpoint.api_protocol,
            requires_api_key=endpoint.api_key is not None,
        )

    def to_endpoint(self, *, api_key: SecretStr | None = None) -> ModelEndpoint:
        return ModelEndpoint(
            model=self.model,
            provider=self.provider,
            base_url=self.base_url,
            api_protocol=self.api_protocol,
            api_key=api_key,
        )


class PersistedNodeOverride(BaseModel):
    """Non-sensitive per-node runtime override persisted on parent WorkflowState."""

    model_config = ConfigDict(frozen=True)

    adapter: str | None = None
    endpoint: PersistedEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool | None = None
    tool_policy: ToolPolicy | None = None
    tools: tuple[str, ...] | None = None


class PersistedRunOptions(BaseModel):
    """Non-sensitive execution configuration persisted for seamless pause/resume inheritance."""

    model_config = ConfigDict(frozen=True)

    adapter: str = "pi"
    profile: str | None = None
    endpoint: PersistedEndpoint | None = None
    thinking_level: ThinkingLevel | None = None
    persist_session: bool = True
    node_overrides: dict[str, PersistedNodeOverride] = Field(default_factory=dict)


class NodeOutput(BaseModel):
    """Structured handoff summary persisted in GraphState for one node in the current turn."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    role: str
    status: Literal["completed", "waiting", "interrupted", "failed", "aborted"]
    summary: str
    # 产出该节点的后端名称（非 Agent 节点为 None），供下游继承会话时校验同构后端
    adapter: str | None = None
    session_ref: str | None = None
    # 记录节点生效的提示词与工具配置，供下游 inherit_session_from 节点跨进程自动继承
    session_settings: dict[str, Any] | None = None
    usage: TokenUsage | None = None
    failure: AgentFailure | None = None
    # 节点因阻塞式交互或进程退出而中断时，保留待用户回复的原始交互请求，供重启后还原弹窗
    pending_interaction: InteractionRequestEvent | None = None


def reduce_node_outputs(
    left: dict[str, NodeOutput],
    right: dict[str, NodeOutput] | None,
) -> dict[str, NodeOutput]:
    """Merge parallel node handoff outputs within a turn; reset to {} when right is None."""
    if right is None:
        return {}
    return {**left, **right}


def merge_node_sessions(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    """Persist per-node session references across turns on the same thread."""
    return {**left, **right}


def reduce_workflow_status(
    left: WorkflowStatus,
    right: WorkflowStatus | None,
) -> WorkflowStatus:
    """Reset to 'running' when right is None; otherwise retain the higher-severity status."""
    if right is None:
        return "running"
    if not left:
        return right
    return right if _STATUS_PRIORITY[right] >= _STATUS_PRIORITY[left] else left


def reduce_failure(
    left: AgentFailure | None,
    right: AgentFailure | None,
) -> AgentFailure | None:
    """Clear on explicit None (turn reset/discard); preserve first failure across parallel steps."""
    if right is None:
        return None
    return left if left is not None else right


def _last_value[T](_left: T, right: T) -> T:
    return right


class AgentChanges(TypedDict):
    """Subgraph output channels emitted by completed AgentNodes without clobbering global status."""

    node_outputs: Annotated[dict[str, NodeOutput], reduce_node_outputs]
    node_sessions: Annotated[dict[str, str], merge_node_sessions]
    latest_output: Annotated[str, _last_value]


class WorkflowState(AgentChanges):
    """Persistent workflow state shared across parent nodes and agent subgraphs."""

    run_id: str
    thread_id: str
    workflow_id: str
    prompt: str
    workspace_dir: str
    run_options: Annotated[PersistedRunOptions | None, _last_value]
    status: Annotated[WorkflowStatus, reduce_workflow_status]
    failure: Annotated[AgentFailure | None, reduce_failure]
