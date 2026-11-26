"""Thread state inspection, WAL reconciliation, and resume planning."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.orchestrator.node import (
    AGENT_RESUME_NODE,
    AGENT_RESUME_PROMPT_KEY,
    APPROVAL_INTERACTION_KEY,
    parse_approval_decision,
)
from agent_runtime.orchestrator.persistence import InflightNodeRecord, InflightNodeStore
from agent_runtime.orchestrator.state import NodeOutput
from agent_runtime.protocol.events import (
    InteractionRequestEvent,
    InteractionResponse,
    PendingInterrupt,
    PendingInterruptKind,
)

ResumedFromReason = Literal["workflow_interrupt", "agent_interaction", "failure", "crash"]


class ResumeInput(BaseModel):
    """单个挂起分支的恢复输入（用于多分支批量并发恢复）。"""

    model_config = ConfigDict(frozen=True)

    resume_value: Any = None
    interaction_response: InteractionResponse | None = None
    prompt: str | None = None


class ThreadState(BaseModel):
    """线程综合状态视图：合并 LangGraph Checkpoint 与 WAL 预写表，供重启后还原 UI 与断点恢复。"""

    model_config = ConfigDict(frozen=True)

    thread_id: str
    status: Literal["completed", "interrupted", "aborted", "failed", "crashed", "running"]
    resumable: bool
    node_outputs: dict[str, NodeOutput] = Field(default_factory=dict)
    node_sessions: dict[str, str] = Field(default_factory=dict)
    next_nodes: tuple[str, ...] = ()
    interrupts: tuple[PendingInterrupt, ...] = ()
    inflight_nodes: tuple[InflightNodeRecord, ...] = ()


@dataclass(frozen=True)
class InspectionResult:
    """Internal result of reconciling LangGraph snapshot state with WAL records."""

    state: ThreadState
    snapshot: StateSnapshot
    paused_outputs: dict[str, NodeOutput]
    paused_sessions: dict[str, str]


@dataclass(frozen=True)
class ResumePlan:
    """Validated LangGraph resume input and optional two-phase crash replay replies."""

    graph_input: Any
    staged_replies: dict[str, Any]
    reason: ResumedFromReason


async def inspect_thread(
    compiled: Any,
    inflight_store: InflightNodeStore,
    thread_id: str,
    *,
    cleanup: bool = False,
) -> InspectionResult:
    """Inspect a thread's checkpoint snapshot and reconcile with inflight WAL records."""
    records = await inflight_store.list_for_thread(thread_id)
    snapshot = await compiled.aget_state(
        {"configurable": {"thread_id": thread_id}}, subgraphs=True
    )
    if not snapshot.values and not snapshot.next:
        raise KeyError(f"No checkpoint found for thread_id '{thread_id}'")

    outputs = dict(snapshot.values.get("node_outputs", {}))
    sessions = dict(snapshot.values.get("node_sessions", {}))
    raw_status = snapshot.values.get("status")
    live: list[InflightNodeRecord] = []
    paused_outputs: dict[str, NodeOutput] = {}
    paused_sessions: dict[str, str] = {}
    interrupts: list[PendingInterrupt] = []
    pending_nodes = set(snapshot.next)
    seen_pending: set[str] = set()

    for task in snapshot.tasks:
        if task.result is not None or task.name not in pending_nodes:
            continue
        seen_pending.add(task.name)
        child = task.state
        if isinstance(child, StateSnapshot):
            if child.next == (AGENT_RESUME_NODE,):
                paused = child.values.get("node_outputs", {}).get(task.name)
                if paused is not None:
                    paused_outputs[paused.node_id] = paused
                    outputs[paused.node_id] = paused
                    child_ref = child.values.get("node_sessions", {}).get(paused.node_id)
                    effective_ref = child_ref or paused.session_ref
                    if effective_ref:
                        paused_sessions[paused.node_id] = effective_ref
                        sessions[paused.node_id] = effective_ref
                    intr_id = task.interrupts[0].id if task.interrupts else None
                    if paused.status == "interrupted":
                        kind: PendingInterruptKind = (
                            "agent_interaction" if paused.pending_interaction else "suspended"
                        )
                    else:
                        kind = paused.status
                    interrupts.append(
                        PendingInterrupt(
                            interrupt_id=intr_id,
                            node_id=paused.node_id,
                            kind=kind,
                            interaction=paused.pending_interaction,
                            failure=paused.failure,
                        )
                    )
                    continue

            pending_ids = {step.id for step in child.tasks}
            matched_wal = False
            for row in records:
                if row.node_id != task.name or row.task_id not in pending_ids:
                    continue
                matched_wal = True
                live.append(row)
                effective_ref = row.session_ref or sessions.get(task.name)
                if effective_ref:
                    sessions[task.name] = effective_ref
                    paused_sessions[task.name] = effective_ref
                outputs[task.name] = NodeOutput(
                    node_id=task.name,
                    role=row.role,
                    status="interrupted",
                    summary="",
                    session_ref=effective_ref,
                    pending_interaction=row.pending_interaction,
                )
                interrupts.append(
                    PendingInterrupt(
                        interrupt_id=None,
                        node_id=task.name,
                        kind="crashed",
                        interaction=row.pending_interaction,
                    )
                )
            if not matched_wal:
                interrupts.append(
                    PendingInterrupt(interrupt_id=None, node_id=task.name, kind="crashed")
                )
            continue

        if task.interrupts:
            for intr in task.interrupts:
                raw_payload = _extract_interrupt_payload((intr,))
                raw_interaction = raw_payload.get(APPROVAL_INTERACTION_KEY)
                if isinstance(raw_interaction, InteractionRequestEvent):
                    interaction: InteractionRequestEvent | None = raw_interaction
                    payload = {
                        k: v for k, v in raw_payload.items() if k != APPROVAL_INTERACTION_KEY
                    }
                else:
                    interaction = None
                    payload = raw_payload
                interrupts.append(
                    PendingInterrupt(
                        interrupt_id=intr.id,
                        node_id=task.name,
                        kind="workflow_interrupt",
                        interaction=interaction,
                        payload=payload,
                    )
                )
        else:
            interrupts.append(
                PendingInterrupt(interrupt_id=None, node_id=task.name, kind="crashed")
            )

    for node_name in snapshot.next:
        if node_name not in seen_pending:
            interrupts.append(
                PendingInterrupt(interrupt_id=None, node_id=node_name, kind="crashed")
            )

    if cleanup:
        for row in records:
            if row not in live:
                await inflight_store.delete(row)

    if interrupts:
        kinds = {item.kind for item in interrupts}
        if "crashed" in kinds:
            status: Literal["completed", "interrupted", "aborted", "failed", "crashed"] = "crashed"
        elif kinds & {"workflow_interrupt", "agent_interaction", "suspended"}:
            status = "interrupted"
        elif "failed" in kinds:
            status = "failed"
        else:
            status = "aborted"
    elif raw_status == "running":
        status = "completed"
    else:
        status = raw_status

    view = ThreadState(
        thread_id=thread_id,
        status=status,
        resumable=bool(snapshot.next),
        node_outputs=outputs,
        node_sessions=sessions,
        next_nodes=snapshot.next,
        interrupts=tuple(interrupts),
        inflight_nodes=tuple(live),
    )
    return InspectionResult(
        state=view,
        snapshot=snapshot,
        paused_outputs=paused_outputs,
        paused_sessions=paused_sessions,
    )


def resolve_resume_plan(
    state: ThreadState,
    *,
    node_id: str | None,
    resume_value: Any,
    interaction_response: InteractionResponse | None,
    prompt: str | None,
    resumes: Mapping[str, ResumeInput] | None,
) -> ResumePlan:
    """将单分支或批量恢复参数解析为 LangGraph `Command(resume=...)` 或崩溃两阶段回放计划。"""
    targets: list[tuple[PendingInterrupt, ResumeInput]] = []
    if resumes is not None:
        if (
            node_id is not None
            or resume_value is not None
            or interaction_response is not None
            or prompt is not None
        ):
            raise ValueError("Cannot combine 'resumes' with single-branch resume parameters")
        if not resumes:
            raise ValueError("'resumes' must specify at least one target node_id")
        for target_node, inp in resumes.items():
            matches = [item for item in state.interrupts if item.node_id == target_node]
            if not matches:
                pending = ", ".join(item.node_id for item in state.interrupts)
                raise ValueError(f"Node '{target_node}' is not paused; pending nodes: [{pending}]")
            if len(matches) > 1:
                raise ValueError(f"Node '{target_node}' has multiple active interrupts")
            targets.append((matches[0], inp))
    else:
        single = ResumeInput(
            resume_value=resume_value,
            interaction_response=interaction_response,
            prompt=prompt,
        )
        if node_id is not None:
            matches = [item for item in state.interrupts if item.node_id == node_id]
            if not matches:
                pending = ", ".join(item.node_id for item in state.interrupts)
                raise ValueError(f"Node '{node_id}' is not paused; pending nodes: [{pending}]")
            if len(matches) > 1:
                raise ValueError(f"Node '{node_id}' has multiple active interrupts")
            targets.append((matches[0], single))
        elif len(state.interrupts) == 1:
            targets.append((state.interrupts[0], single))
        elif interaction_response is not None:
            matches = [
                item
                for item in state.interrupts
                if item.interaction is not None
                and item.interaction.request_id == interaction_response.request_id
            ]
            if len(matches) == 1:
                targets.append((matches[0], single))
            elif len(matches) > 1:
                nodes = ", ".join(item.node_id for item in matches)
                raise ValueError(
                    f"Multiple pending nodes share request_id "
                    f"'{interaction_response.request_id}' ({nodes}); specify node_id"
                )
            else:
                raise ValueError("Interaction request_id does not match any pending request")
        elif (
            all(item.kind == "crashed" for item in state.interrupts)
            and not state.inflight_nodes
            and resume_value is None
            and not (prompt and prompt.strip())
        ):
            targets = [(item, single) for item in state.interrupts]
        else:
            pending = ", ".join(item.node_id for item in state.interrupts)
            raise ValueError(
                f"Multiple nodes are paused ({pending}); specify node_id, "
                "a matching interaction_response, or resumes"
            )

    wal_nodes = {row.node_id for row in state.inflight_nodes}
    needs_crash_replay = state.status == "crashed"
    resume_map: dict[str, Any] = {}
    staged_replies: dict[str, Any] = {}

    for item, inp in targets:
        if item.kind == "workflow_interrupt":
            if item.interaction is not None:
                if inp.interaction_response is not None:
                    if inp.interaction_response.request_id != item.interaction.request_id:
                        raise ValueError(
                            "Interaction request_id does not match the pending request"
                        )
                    raw_decision: Any = inp.interaction_response.model_dump()
                elif inp.resume_value is not None:
                    raw_decision = inp.resume_value
                else:
                    raw_decision = inp.prompt
                parsed_decision, parsed_feedback = parse_approval_decision(
                    raw_decision, node_id=item.node_id
                )
                decision: Any = {"decision": parsed_decision, "feedback": parsed_feedback}
            else:
                decision = (
                    inp.resume_value
                    if inp.resume_value is not None
                    else (
                        inp.interaction_response.model_dump()
                        if inp.interaction_response is not None
                        else inp.prompt
                    )
                )
                if decision is None:
                    raise ValueError(
                        f"Workflow interrupt on '{item.node_id}' requires a resume value"
                    )
            if needs_crash_replay:
                staged_replies[item.node_id] = decision
            else:
                assert item.interrupt_id is not None
                resume_map[item.interrupt_id] = decision
        elif item.kind == "crashed":
            text = format_resume_prompt(
                pending_interaction=item.interaction,
                interaction_response=inp.interaction_response,
                prompt=inp.prompt,
            )
            if item.node_id in wal_nodes:
                staged_replies[item.node_id] = {AGENT_RESUME_PROMPT_KEY: text}
            elif text is not None:
                raise ValueError(
                    "Cannot pass a continuation prompt when resuming a crash with no started "
                    "agent turn"
                )
        else:
            text = format_resume_prompt(
                pending_interaction=item.interaction,
                interaction_response=inp.interaction_response,
                prompt=inp.prompt,
            )
            payload = {AGENT_RESUME_PROMPT_KEY: text}
            if needs_crash_replay:
                staged_replies[item.node_id] = payload
            else:
                assert item.interrupt_id is not None
                resume_map[item.interrupt_id] = payload

    target_kinds = {item.kind for item, _ in targets}
    if needs_crash_replay or "crashed" in target_kinds:
        reason: ResumedFromReason = "crash"
    elif "workflow_interrupt" in target_kinds:
        reason = "workflow_interrupt"
    elif target_kinds & {"agent_interaction", "suspended"}:
        reason = "agent_interaction"
    else:
        reason = "failure"

    graph_input: Any = None if needs_crash_replay else Command(resume=resume_map)
    return ResumePlan(graph_input=graph_input, staged_replies=staged_replies, reason=reason)


def format_resume_prompt(
    *,
    pending_interaction: InteractionRequestEvent | None,
    interaction_response: InteractionResponse | None,
    prompt: str | None,
) -> str | None:
    """将冷恢复时的用户审批裁决或问题回答格式化为续接指令（同时支持直接传入补充 prompt）。"""
    if pending_interaction is not None and interaction_response is None:
        raise ValueError(
            f"A response for request_id '{pending_interaction.request_id}' is required"
        )
    if interaction_response is not None and (
        pending_interaction is None
        or interaction_response.request_id != pending_interaction.request_id
    ):
        raise ValueError("Interaction request_id does not match the pending request")
    parts: list[str] = []
    if interaction_response is not None:
        assert pending_interaction is not None
        title = pending_interaction.title or "request"
        if pending_interaction.kind == "user_input":
            if interaction_response.decision != "approve":
                parts.append(f"The user cancelled the question ({title}).")
            else:
                if interaction_response.value is None:
                    raise ValueError("Answering a pending question requires a value")
                answer = interaction_response.value
                parts.append(f"The user answered your question ({title}): {answer}")
        else:
            detail = f"decision={interaction_response.decision}"
            if interaction_response.feedback:
                detail = f"{detail}, feedback={interaction_response.feedback}"
            parts.append(f"The user responded to your approval request ({title}): {detail}.")
        parts.append("Continue your task accordingly.")

    if prompt and prompt.strip():
        parts.append(prompt.strip())

    return "\n".join(parts) if parts else None


def _extract_interrupt_payload(raw_interrupts: Any) -> dict[str, Any]:
    """从 LangGraph `__interrupt__` 元组中归一化提取字典载荷。"""
    if isinstance(raw_interrupts, (list, tuple)) and raw_interrupts:
        first = raw_interrupts[0]
        value = getattr(first, "value", first)
        if isinstance(value, dict):
            return dict(value)
        return {"value": value}
    if isinstance(raw_interrupts, dict):
        return dict(raw_interrupts)
    return {"value": raw_interrupts}
