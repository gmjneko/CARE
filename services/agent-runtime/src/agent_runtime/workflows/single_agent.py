"""Single-node general-purpose agent workflow (START -> agent -> END)."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent_runtime.orchestrator import (
    AgentNodeConfig,
    WorkflowDefinition,
    WorkflowState,
    build_agent_node,
)


def build_single_agent_workflow() -> StateGraph[WorkflowState]:
    """1-node Vibe Coding graph: START -> agent -> END."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "agent",
        build_agent_node(
            AgentNodeConfig(
                id="agent",
                role="general",
                prompt_builder=lambda state: state["prompt"],
            )
        ),
    )
    graph.add_edge(START, "agent")
    graph.add_edge("agent", END)
    return graph


SINGLE_AGENT_WORKFLOW = WorkflowDefinition(
    name="single_agent",
    description="Single general-purpose agent node (START -> agent -> END).",
    build=build_single_agent_workflow,
)
