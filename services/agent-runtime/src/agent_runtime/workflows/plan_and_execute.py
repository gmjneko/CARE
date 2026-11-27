"""Interactive plan-and-execute workflow: planner -> context-inheriting executor."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent_runtime.orchestrator import (
    AgentNodeConfig,
    WorkflowDefinition,
    WorkflowState,
    build_agent_node,
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


def _stay_interactive(_output: object, _tools: object) -> bool:
    return False


def _build_planner_prompt(state: WorkflowState) -> str:
    return (
        f"{PLAN_PHASE_BANNER}\n\n"
        "Analyze the workspace and collaborate on a clear execution plan for:\n\n"
        f"{state['prompt']}"
    )


def _build_executor_prompt(_state: WorkflowState) -> str:
    # executor 已继承 planner 完整上下文，此处用对等横幅声明阶段切换
    return (
        f"{EXECUTE_PHASE_BANNER}\n\n"
        "Follow the plan above carefully, verify your work, "
        "and summarize what you did."
    )


def build_plan_and_execute_workflow() -> StateGraph[WorkflowState]:
    """2-node interactive workflow: planner -> executor (forks planner session) -> END."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)

    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                prompt_builder=_build_planner_prompt,
                should_complete=_stay_interactive,
            )
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
                should_complete=_stay_interactive,
            )
        ),
    )

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", END)
    return graph


PLAN_AND_EXECUTE_WORKFLOW = WorkflowDefinition(
    name="plan_and_execute",
    description=(
        "Interactive Plan phase followed by interactive Execute phase inheriting planner context "
        "(START -> planner -> executor -> END)."
    ),
    build=build_plan_and_execute_workflow,
)
