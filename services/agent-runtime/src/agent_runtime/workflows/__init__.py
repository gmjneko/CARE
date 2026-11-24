"""Built-in code-defined LangGraph workflows and default registry factory."""

from agent_runtime.orchestrator import WorkflowDefinition, WorkflowRegistry
from agent_runtime.workflows.plan_and_execute import (
    PLAN_AND_EXECUTE_WORKFLOW,
    build_plan_and_execute_workflow,
)
from agent_runtime.workflows.single_agent import (
    SINGLE_AGENT_WORKFLOW,
    build_single_agent_workflow,
)

BUILTIN_WORKFLOWS: tuple[WorkflowDefinition, ...] = (
    SINGLE_AGENT_WORKFLOW,
    PLAN_AND_EXECUTE_WORKFLOW,
)


def create_default_workflow_registry() -> WorkflowRegistry:
    """Create a WorkflowRegistry pre-populated with all built-in workflows."""
    return WorkflowRegistry(BUILTIN_WORKFLOWS)


__all__ = [
    "BUILTIN_WORKFLOWS",
    "PLAN_AND_EXECUTE_WORKFLOW",
    "SINGLE_AGENT_WORKFLOW",
    "build_plan_and_execute_workflow",
    "build_single_agent_workflow",
    "create_default_workflow_registry",
]
