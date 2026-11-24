"""WorkflowDefinition metadata and explicit WorkflowRegistry container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from langgraph.graph import StateGraph

from agent_runtime.adapters.base import ModelEndpoint, ThinkingLevel
from agent_runtime.orchestrator.node import RunContext
from agent_runtime.orchestrator.state import WorkflowState


class WorkflowBuilder(Protocol):
    """Callable building an uncompiled LangGraph StateGraph bound to `run_context`."""

    def __call__(
        self,
        run_context: RunContext,
        *,
        adapter: str = "pi",
        endpoint: ModelEndpoint | None = None,
        thinking_level: ThinkingLevel | None = None,
        persist_session: bool = True,
    ) -> StateGraph[WorkflowState]: ...


@dataclass(frozen=True)
class WorkflowDefinition:
    """Self-contained descriptor for a named code-defined workflow."""

    name: str
    description: str
    build: WorkflowBuilder


class WorkflowRegistry:
    """Explicit dependency container mapping workflow names to WorkflowDefinitions."""

    def __init__(self, definitions: tuple[WorkflowDefinition, ...] = ()) -> None:
        self._definitions: dict[str, WorkflowDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: WorkflowDefinition) -> None:
        if not definition.name:
            raise ValueError("Workflow name must be a non-empty string")
        if definition.name in self._definitions:
            raise ValueError(f"Workflow '{definition.name}' is already registered")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> WorkflowDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._definitions)) or "<none>"
            raise KeyError(f"Unknown workflow '{name}'. Registered workflows: {available}") from exc

    def list_definitions(self) -> tuple[WorkflowDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def list_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))
