"""Compact LangGraph workflow state avoiding cross-node context bloat."""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from agent_runtime.protocol.events import AgentFailure, InteractionRequestEvent, TokenUsage


class NodeOutput(BaseModel):
    """Structured handoff summary persisted in GraphState for one completed AgentNode."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    role: str
    status: Literal["completed", "interrupted", "failed", "aborted"]
    summary: str
    session_ref: str | None = None
    usage: TokenUsage | None = None
    failure: AgentFailure | None = None
    # 节点因阻塞式交互或进程退出而中断时，保留待用户回复的原始交互请求，供重启后还原弹窗
    pending_interaction: InteractionRequestEvent | None = None


def merge_node_outputs(
    left: dict[str, NodeOutput], right: dict[str, NodeOutput]
) -> dict[str, NodeOutput]:
    return {**left, **right}


class WorkflowState(TypedDict):
    """Minimal state carried across LangGraph nodes and persisted in checkpointers.

    Raw token deltas and verbose Pi transcripts stay on the ephemeral event stream
    and are deliberately excluded from WorkflowState.
    """

    run_id: str
    thread_id: str
    workflow_id: str
    prompt: str
    workspace_dir: str
    node_outputs: Annotated[dict[str, NodeOutput], merge_node_outputs]
    latest_output: str
    status: Literal["running", "interrupted", "completed", "failed", "aborted"]
    failure: AgentFailure | None
    # 仅在 runner.resume() 恢复中断/失败/崩溃线程时生效；恢复节点成功完成后即清空
    resume_mode: bool
    resume_prompt: str | None
