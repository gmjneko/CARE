"""Two-node sequential workflow: read-only planner followed by full-tool executor."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent_runtime.adapters.base import ModelEndpoint, ThinkingLevel
from agent_runtime.orchestrator import (
    AgentNodeConfig,
    RunContext,
    WorkflowDefinition,
    WorkflowState,
    build_agent_node,
    continue_if_running,
)


def _build_executor_prompt(state: WorkflowState) -> str:
    planner_output = state["node_outputs"].get("planner")
    if planner_output is None or not planner_output.summary:
        return state["prompt"]
    return (
        f"## User Prompt\n{state['prompt']}\n\n"
        f"## Execution Plan (from planner)\n{planner_output.summary}\n\n"
        "Follow the plan above carefully, verify your work, and summarize what you did."
    )


def build_plan_and_execute_workflow(
    run_context: RunContext,
    *,
    adapter: str = "pi",
    endpoint: ModelEndpoint | None = None,
    thinking_level: ThinkingLevel | None = None,
    persist_session: bool = True,
) -> StateGraph[WorkflowState]:
    """2-node sequential workflow: planner (read-only) -> executor (full tools) -> END."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)

    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                adapter=adapter,
                endpoint=endpoint,
                thinking_level=thinking_level,
                persist_session=persist_session,
                tools=("read", "grep", "find", "ls"),
                append_system_prompt=(
                    "You are the Planner agent in a multi-agent workflow. "
                    "Inspect the workspace using read-only tools and produce a concise, "
                    "actionable step-by-step plan for the Executor agent. Do not modify files."
                ),
                prompt_builder=lambda state: (
                    "Analyze the workspace and create a clear execution plan for:\n\n"
                    f"{state['prompt']}"
                ),
            ),
            run_context,
        ),
    )

    graph.add_node(
        "executor",
        build_agent_node(
            AgentNodeConfig(
                id="executor",
                role="executor",
                adapter=adapter,
                endpoint=endpoint,
                thinking_level=thinking_level,
                persist_session=persist_session,
                append_system_prompt=(
                    "You are the Executor agent in a multi-agent workflow. "
                    "Execute the Planner's plan accurately in the workspace and verify the result."
                ),
                prompt_builder=_build_executor_prompt,
            ),
            run_context,
        ),
    )

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        continue_if_running("executor"),
        {"executor": "executor", END: END},
    )
    graph.add_edge("executor", END)
    return graph


PLAN_AND_EXECUTE_WORKFLOW = WorkflowDefinition(
    name="plan_and_execute",
    description=("Read-only planner followed by full-tool executor (planner -> executor -> END)."),
    build=build_plan_and_execute_workflow,
)
