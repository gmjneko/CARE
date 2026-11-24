"""Minimal runnable entry point to exercise CARE Workflow + Agent (Pi RPC) end-to-end."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import cast

from agent_runtime.adapters import AdapterRegistry, PiRpcAdapter, PiRpcConfig, ThinkingLevel
from agent_runtime.orchestrator import WorkflowRegistry, WorkflowRunner
from agent_runtime.protocol import (
    NodeCompletedEvent,
    NodeStartedEvent,
    RuntimeEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    WorkflowCompletedEvent,
    WorkflowStartedEvent,
)
from agent_runtime.workflows import create_default_workflow_registry


def _parse_args(
    workflow_registry: WorkflowRegistry,
    argv: list[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a CARE LangGraph workflow backed by Pi RPC.")
    parser.add_argument(
        "--list-workflows",
        action="store_true",
        help="List registered workflows and exit.",
    )
    parser.add_argument(
        "--workflow",
        default="single_agent",
        choices=workflow_registry.list_names(),
        help="Workflow to execute (default: single_agent).",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Workspace directory passed to agent sessions (default: current directory).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override passed to Pi.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Optional provider override (requires --model).",
    )
    parser.add_argument(
        "--thinking",
        default=None,
        choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
        help="Optional thinking level override.",
    )
    parser.add_argument(
        "--pi-bin",
        default=os.environ.get("PI_BIN", "pi"),
        help="Path or command name for Pi CLI (default: $PI_BIN or 'pi').",
    )
    parser.add_argument(
        "--checkpoint-db",
        default=":memory:",
        help="SQLite path for LangGraph checkpoints (default: in-memory).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit raw RuntimeEvent JSONL records instead of formatted terminal output.",
    )
    parser.add_argument(
        "goal",
        nargs="?",
        help="Task goal prompt for the workflow.",
    )
    return parser.parse_args(argv)


def _render_human(event: RuntimeEvent, state: dict[str, str | None]) -> None:
    payload = event.payload

    def _end_stream() -> None:
        if state["stream"] is not None:
            print(flush=True)
            state["stream"] = None

    if isinstance(payload, WorkflowStartedEvent):
        print(f"[workflow.started] workflow={payload.preset} run_id={event.run_id}", flush=True)
    elif isinstance(payload, NodeStartedEvent):
        _end_stream()
        print(
            f"\n[node.started] node={payload.node_id} "
            f"role={payload.role} adapter={payload.adapter}",
            flush=True,
        )
    elif isinstance(payload, ThinkingDeltaEvent):
        if state["stream"] != "thinking":
            _end_stream()
            print("[thinking] ", end="", flush=True)
            state["stream"] = "thinking"
        print(payload.delta, end="", flush=True)
    elif isinstance(payload, TextDeltaEvent):
        if state["stream"] != "text":
            _end_stream()
            state["stream"] = "text"
        print(payload.delta, end="", flush=True)
    elif isinstance(payload, ToolStartedEvent):
        _end_stream()
        print(f"[tool.started] {payload.tool_name}({payload.arguments})", flush=True)
    elif isinstance(payload, ToolOutputDeltaEvent):
        if state["stream"] != "tool":
            _end_stream()
            state["stream"] = "tool"
        print(payload.chunk, end="", flush=True)
    elif isinstance(payload, ToolCompletedEvent):
        _end_stream()
        status = "ERROR" if payload.is_error else "OK"
        duration = f" {payload.duration_ms}ms" if payload.duration_ms is not None else ""
        print(f"[tool.completed] {payload.tool_name} [{status}{duration}]", flush=True)
    elif isinstance(payload, NodeCompletedEvent):
        _end_stream()
        usage = f" tokens={payload.usage.total_tokens}" if payload.usage else ""
        print(f"[node.completed] node={payload.node_id} status={payload.status}{usage}", flush=True)
        if payload.failure:
            print(
                f"  failure=({payload.failure.failure_class}) {payload.failure.message}",
                flush=True,
            )
    elif isinstance(payload, WorkflowCompletedEvent):
        _end_stream()
        print(f"\n[workflow.completed] status={payload.status} seq={event.seq}", flush=True)
        if payload.failure:
            print(
                f"  failure=({payload.failure.failure_class}) {payload.failure.message}",
                flush=True,
            )


async def _run_async(
    args: argparse.Namespace,
    workflow_registry: WorkflowRegistry,
) -> int:
    adapter_registry = AdapterRegistry({"pi": PiRpcAdapter(PiRpcConfig(command=(args.pi_bin,)))})

    render_state: dict[str, str | None] = {"stream": None}
    exit_code = 0

    async with WorkflowRunner(
        adapter_registry,
        workflow_registry,
        checkpoint_db=args.checkpoint_db,
    ) as runner:
        async for event in runner.run(
            args.workflow,
            args.goal,
            Path(args.cwd),
            model=args.model,
            provider=args.provider,
            thinking_level=cast(ThinkingLevel | None, args.thinking),
        ):
            if args.json_output:
                print(event.model_dump_json(), flush=True)
            else:
                _render_human(event, render_state)

            if isinstance(event.payload, WorkflowCompletedEvent) and (
                event.payload.status != "completed"
            ):
                exit_code = 1

    return exit_code


def main(argv: list[str] | None = None) -> int:
    # Ensure UTF-8 console output on Windows terminals.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    workflow_registry = create_default_workflow_registry()
    args = _parse_args(workflow_registry, argv)
    if args.list_workflows:
        for definition in workflow_registry.list_definitions():
            print(f"{definition.name}: {definition.description}")
        return 0

    if not args.goal:
        print("Error: a task goal prompt is required (or pass --list-workflows).", file=sys.stderr)
        return 2

    return asyncio.run(_run_async(args, workflow_registry))


if __name__ == "__main__":
    raise SystemExit(main())
