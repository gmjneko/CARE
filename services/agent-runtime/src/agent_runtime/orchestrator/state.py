"""Compact LangGraph workflow state avoiding cross-node context bloat."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from agent_runtime.protocol.events import AgentFailure, InteractionRequestEvent, TokenUsage

WorkflowStatus = Literal["running", "interrupted", "completed", "failed", "aborted"]

_STATUS_PRIORITY: dict[WorkflowStatus, int] = {
    "running": 0,
    "completed": 1,
    "interrupted": 2,
    "aborted": 3,
    "failed": 4,
}


class NodeOutput(BaseModel):
    """Structured handoff summary persisted in GraphState for one node in the current turn."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    role: str
    status: Literal["completed", "waiting", "interrupted", "failed", "aborted"]
    summary: str
    session_ref: str | None = None
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
    status: Annotated[WorkflowStatus, reduce_workflow_status]
    failure: Annotated[AgentFailure | None, reduce_failure]
