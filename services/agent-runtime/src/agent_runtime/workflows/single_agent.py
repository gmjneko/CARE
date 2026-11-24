"""Single-node general-purpose agent workflow (START -> agent -> END)."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent_runtime.adapters.base import ThinkingLevel
from agent_runtime.orchestrator import (
    AgentNodeConfig,
    RunContext,
    WorkflowDefinition,
    WorkflowState,
    build_agent_node,
)


def build_single_agent_workflow(
    run_context: RunContext,
    *,
    adapter: str = "pi",
    model: str | None = None,
    provider: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> StateGraph[WorkflowState]:
    """1-node Vibe Coding graph: START -> agent -> END."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "agent",
        build_agent_node(
            AgentNodeConfig(
                id="agent",
                role="general",
                adapter=adapter,
                model=model,
                provider=provider,
                thinking_level=thinking_level,
                prompt_builder=lambda state: state["goal"],
            ),
            run_context,
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
