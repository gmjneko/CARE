"""Runnable entry point to exercise CARE Workflow + Agent (Pi RPC) end-to-end."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

from agent_runtime.adapters import AdapterRegistry, PiRpcAdapter, PiRpcConfig, ThinkingLevel
from agent_runtime.orchestrator import WorkflowRunner
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

WorkflowName = Literal["single_agent", "plan_and_execute"]


def main() -> int:
    """IDE 直接右键运行入口，按需修改下方参数即可。"""
    return run(
        # 用户提示词（直接在这里修改）
        prompt="列出当前目录的文件，并简短概括项目结构。",
        # 内置工作流: "single_agent" | "plan_and_execute"
        workflow="single_agent",
        # Agent 工作目录（默认当前目录，支持相对路径、绝对路径或 ~）
        cwd=".",
        # 可选模型与推理覆盖（None 表示沿用 Pi 默认配置）
        model=None,
        provider=None,  # 指定 provider 时必须同时指定 model
        thinking_level=None,  # "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"
        # 运行时配置
        pi_bin=None,  # None 时读取环境变量 PI_BIN，未设置则默认 "pi"
        checkpoint_db=":memory:",  # ":memory:" 或 SQLite 路径，如 "checkpoints.sqlite"
        json_output=False,  # True 时输出原始 RuntimeEvent JSONL
    )


def run(
    prompt: str,
    *,
    workflow: WorkflowName | str = "single_agent",
    cwd: str | Path = ".",
    model: str | None = None,
    provider: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    pi_bin: str | None = None,
    checkpoint_db: str | Path = ":memory:",
    json_output: bool = False,
) -> int:
    """Run a CARE workflow synchronously and stream events to stdout."""
    if not prompt.strip():
        raise ValueError("prompt must be non-empty")

    # Ensure UTF-8 console output on Windows terminals.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    effective_pi_bin = pi_bin or os.environ.get("PI_BIN", "pi")
    workspace_dir = Path(cwd).expanduser()

    async def _execute() -> int:
        workflow_registry = create_default_workflow_registry()
        adapter_registry = AdapterRegistry(
            {"pi": PiRpcAdapter(PiRpcConfig(command=(effective_pi_bin,)))}
        )

        render_state: dict[str, str | None] = {"stream": None}
        exit_code = 0

        async with WorkflowRunner(
            adapter_registry,
            workflow_registry,
            checkpoint_db=checkpoint_db,
        ) as runner:
            async for event in runner.run(
                workflow,
                prompt,
                workspace_dir,
                model=model,
                provider=provider,
                thinking_level=thinking_level,
            ):
                if json_output:
                    print(event.model_dump_json(), flush=True)
                else:
                    _render_human(event, render_state)

                if isinstance(event.payload, WorkflowCompletedEvent) and (
                    event.payload.status != "completed"
                ):
                    exit_code = 1

        return exit_code

    return asyncio.run(_execute())


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


if __name__ == "__main__":
    raise SystemExit(main())
