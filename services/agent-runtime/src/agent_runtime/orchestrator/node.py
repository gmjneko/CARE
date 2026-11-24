"""Generic LangGraph AgentNode wrapping any AgentAdapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END

from agent_runtime.adapters.base import (
    AgentSession,
    InteractionResponse,
    SessionConfig,
    ThinkingLevel,
)
from agent_runtime.adapters.errors import AdapterProtocolError
from agent_runtime.adapters.registry import AdapterRegistry
from agent_runtime.orchestrator.state import NodeOutput, WorkflowState
from agent_runtime.protocol.events import (
    AgentEvent,
    InteractionRequestEvent,
    NodeCompletedEvent,
    NodeStartedEvent,
    TurnCompletedEvent,
    WorkflowEvent,
)

InteractionHandler = Callable[[InteractionRequestEvent], Awaitable[InteractionResponse]]
PromptBuilder = Callable[[WorkflowState], str]


@dataclass(frozen=True)
class StreamPacket:
    """Internal packet written to LangGraph's custom stream writer."""

    node_id: str
    event: AgentEvent | WorkflowEvent


@dataclass(frozen=True)
class AgentNodeConfig:
    """Code-first configuration for a single LangGraph node backed by an AgentAdapter."""

    id: str
    role: str
    prompt_builder: PromptBuilder
    adapter: str = "pi"
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    tools: tuple[str, ...] | None = None
    no_tools: bool = False
    model: str | None = None
    provider: str | None = None
    thinking_level: ThinkingLevel | None = None
    extensions: tuple[Path, ...] = ()
    persist_session: bool = False


@dataclass
class RunContext:
    """Shared runtime state injected into compiled LangGraph agent nodes."""

    registry: AdapterRegistry
    active_sessions: dict[str, AgentSession] = field(default_factory=dict)
    interaction_handlers: dict[str, InteractionHandler] = field(default_factory=dict)


def build_agent_node(
    node_config: AgentNodeConfig,
    run_context: RunContext,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Create an async LangGraph node function bound to `node_config` and `run_context`."""

    async def _agent_node(state: WorkflowState) -> dict[str, Any]:
        if state["status"] != "running":
            return {}

        run_id = state["run_id"]
        adapter = run_context.registry.get(node_config.adapter)
        session_config = SessionConfig(
            cwd=Path(state["workspace_dir"]),
            model=node_config.model,
            provider=node_config.provider,
            thinking_level=node_config.thinking_level,
            system_prompt=node_config.system_prompt,
            append_system_prompt=node_config.append_system_prompt,
            tools=node_config.tools,
            no_tools=node_config.no_tools,
            extensions=node_config.extensions,
            persist_session=node_config.persist_session,
        )

        writer = get_stream_writer()
        writer(
            StreamPacket(
                node_id=node_config.id,
                event=NodeStartedEvent(
                    node_id=node_config.id,
                    role=node_config.role,
                    adapter=node_config.adapter,
                ),
            )
        )

        prompt = node_config.prompt_builder(state)
        terminal: TurnCompletedEvent | None = None
        session_ref: str | None = None

        async with adapter.open_session(session_config) as session:
            run_context.active_sessions[run_id] = session
            try:
                async for event in session.run_turn(prompt):
                    writer(StreamPacket(node_id=node_config.id, event=event))

                    if isinstance(event, InteractionRequestEvent) and event.blocking:
                        handler = run_context.interaction_handlers.get(run_id)
                        if handler is not None:
                            response = await handler(event)
                        else:
                            # Cancel blocking interaction requests immediately in headless mode
                            # so the child agent never hangs indefinitely.
                            response = InteractionResponse(
                                request_id=event.request_id, decision="cancel"
                            )
                        await session.respond_interaction(response)

                    if isinstance(event, TurnCompletedEvent):
                        terminal = event
                session_ref = session.session_ref
            finally:
                if run_context.active_sessions.get(run_id) is session:
                    del run_context.active_sessions[run_id]

        if terminal is None:
            raise AdapterProtocolError(
                f"Agent node '{node_config.id}' ended turn without TurnCompletedEvent"
            )

        output = NodeOutput(
            node_id=node_config.id,
            role=node_config.role,
            status=terminal.status,
            summary=terminal.final_text,
            session_ref=session_ref,
            usage=terminal.usage,
            failure=terminal.failure,
        )

        writer(
            StreamPacket(
                node_id=node_config.id,
                event=NodeCompletedEvent(
                    node_id=node_config.id,
                    role=node_config.role,
                    status=terminal.status,
                    summary=terminal.final_text,
                    usage=terminal.usage,
                    failure=terminal.failure,
                ),
            )
        )

        next_status = "running" if terminal.status == "completed" else terminal.status
        return {
            "node_outputs": {node_config.id: output},
            "latest_output": terminal.final_text
            if terminal.status == "completed"
            else state["latest_output"],
            "status": next_status,
            "failure": terminal.failure,
        }

    _agent_node.__name__ = f"agent_node_{node_config.id}"
    return _agent_node


def continue_if_running(next_node: str) -> Callable[[WorkflowState], str]:
    """Conditional edge router that halts downstream nodes as soon as a node fails or aborts."""

    def _route(state: WorkflowState) -> str:
        return next_node if state["status"] == "running" else END

    return _route
