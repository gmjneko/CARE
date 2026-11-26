"""Plan-approve-execute workflow: read-only planner, approval gate, then full-tool executor."""

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


def _build_executor_prompt(state: WorkflowState) -> str:
    planner_output = state["node_outputs"].get("planner")
    if planner_output is None or not planner_output.summary:
        return state["prompt"]
    approval_output = state["node_outputs"].get("plan_approval")
    approval_note = ""
    if approval_output is not None and approval_output.summary.startswith("approved: "):
        note = approval_output.summary.removeprefix("approved: ").strip()
        if note:
            approval_note = f"\n\n## Approval Note\n{note}"
    return (
        f"## User Prompt\n{state['prompt']}\n\n"
        f"## Execution Plan (from planner)\n{planner_output.summary}"
        f"{approval_note}\n\n"
        "Follow the plan above carefully, verify your work, and summarize what you did."
    )


def build_plan_and_execute_workflow() -> StateGraph[WorkflowState]:
    """3-node workflow: planner (read-only) -> plan_approval -> executor (full tools) -> END."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)

    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                tool_policy="read_only",
                append_system_prompt=(
                    "You are the Planner agent in a multi-agent workflow. "
                    "Inspect the workspace using read-only tools and produce a concise, "
                    "actionable step-by-step plan for the Executor agent. Do not modify files."
                ),
                prompt_builder=lambda state: (
                    "Analyze the workspace and create a clear execution plan for:\n\n"
                    f"{state['prompt']}"
                ),
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
                append_system_prompt=(
                    "You are the Executor agent in a multi-agent workflow. "
                    "Execute the Planner's plan accurately in the workspace and verify the result."
                ),
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
        "Read-only planner, human plan approval gate, then full-tool executor "
        "(planner -> plan_approval -> executor -> END)."
    ),
    build=build_plan_and_execute_workflow,
)
