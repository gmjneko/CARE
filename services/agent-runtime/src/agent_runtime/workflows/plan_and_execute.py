"""Plan-approve-execute workflow: planner, approval gate, then context-inheriting executor."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent_runtime.orchestrator import (
    AgentNodeConfig,
    WorkflowDefinition,
    WorkflowState,
    build_agent_node,
    build_approval_node,
    continue_if_running,
)

PLAN_PHASE_BANNER = (
    "# ---------------------------------------------------------------------------\n"
    "#  Currently in the **Plan** phase, you can only use **Read-Only** tools.\n"
    "# ---------------------------------------------------------------------------"
)

EXECUTE_PHASE_BANNER = (
    "# ---------------------------------------------------------------------------\n"
    "#  Currently in the **Execute** phase, you can now use **All** tools.\n"
    "# ---------------------------------------------------------------------------"
)


def _build_planner_prompt(state: WorkflowState) -> str:
    return (
        f"{PLAN_PHASE_BANNER}\n\n"
        "Analyze the workspace and create a clear execution plan for:\n\n"
        f"{state['prompt']}"
    )


def _build_executor_prompt(state: WorkflowState) -> str:
    # executor 已继承 planner 完整上下文，此处用对等横幅声明阶段切换并拼接可选审批备注
    approval_output = state["node_outputs"].get("plan_approval")
    approval_note = ""
    if approval_output is not None and approval_output.summary.startswith("approved: "):
        note = approval_output.summary.removeprefix("approved: ").strip()
        if note:
            approval_note = f"\n\n## Approval Note\n{note}"
    return (
        f"{EXECUTE_PHASE_BANNER}\n\n"
        "Follow the approved plan above carefully, verify your work, "
        "and summarize what you did."
        f"{approval_note}"
    )


def build_plan_and_execute_workflow() -> StateGraph[WorkflowState]:
    """3-node workflow: planner -> plan_approval -> executor (forks planner session) -> END."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)

    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                prompt_builder=_build_planner_prompt,
            )
        ),
    )

    graph.add_node(
        "plan_approval",
        build_approval_node(
            "plan_approval",
            title="确认执行计划",
            message="是否按照plan执行",
            content_builder=lambda state: (
                state["node_outputs"]["planner"].summary
                if "planner" in state["node_outputs"]
                else state["latest_output"]
            ),
        ),
    )

    graph.add_node(
        "executor",
        build_agent_node(
            AgentNodeConfig(
                id="executor",
                role="executor",
                inherit_session_from="planner",
                prompt_builder=_build_executor_prompt,
            )
        ),
    )

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "plan_approval")
    graph.add_conditional_edges(
        "plan_approval",
        continue_if_running("executor"),
        {"executor": "executor", END: END},
    )
    graph.add_edge("executor", END)
    return graph


PLAN_AND_EXECUTE_WORKFLOW = WorkflowDefinition(
    name="plan_and_execute",
    description=(
        "Plan phase prompt, human plan approval gate, then executor inheriting planner context "
        "(START -> planner -> plan_approval -> executor -> END)."
    ),
    build=build_plan_and_execute_workflow,
)
