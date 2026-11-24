"""Runnable entry point to exercise CARE Workflow + Agent (Pi RPC) end-to-end."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Literal

from agent_runtime.adapters import (
    AdapterRegistry,
    ModelEndpoint,
    PiRpcAdapter,
    PiRpcConfig,
    ThinkingLevel,
)
from agent_runtime.config import load_local_settings
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
    """IDE 直接右键运行入口，按需修改下方参数即可。

    模型 / Base URL / API Key 默认读取 `~/.care/settings.json`（可通过 `CARE_HOME` 覆盖目录），
    未配置时沿用 Pi 本机默认配置；单次临时测试也可直接传 `endpoint=ModelEndpoint(...)`。
    """
    return run(
        # 用户提示词（直接在这里修改）
        prompt="列出当前目录的文件，并简短概括项目结构。",
        # 内置工作流: "single_agent" | "plan_and_execute"
        workflow="single_agent",
        # Agent 工作目录（默认当前目录，支持相对路径、绝对路径或 ~）
        cwd=".",
        # 使用 ~/.care/settings.json 中的配置名（None 表示使用 active_profile）
        profile=None,
        # 可选单次临时覆盖（与 profile 二选一；None 时沿用 profile 或 Pi 默认配置）
        endpoint=None,
        thinking_level=None,  # "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"
        # 运行时配置
        pi_bin=None,  # None 时依次读取 settings.json -> $PI_BIN -> "pi"
        checkpoint_db=":memory:",  # ":memory:" 或 SQLite 路径，如 "checkpoints.sqlite"
        json_output=False,  # True 时输出原始 RuntimeEvent JSONL
    )


def run(
    prompt: str,
    *,
    workflow: WorkflowName | str = "single_agent",
    cwd: str | Path = ".",
    profile: str | None = None,
    endpoint: ModelEndpoint | None = None,
    thinking_level: ThinkingLevel | None = None,
    pi_bin: str | None = None,
    checkpoint_db: str | Path = ":memory:",
    json_output: bool = False,
) -> int:
    """Run a CARE workflow synchronously and stream events to stdout."""
    if not prompt.strip():
        raise ValueError("prompt must be non-empty")
    if profile is not None and endpoint is not None:
        raise ValueError("Cannot specify both 'profile' and 'endpoint'")

    # Ensure UTF-8 console output on Windows terminals.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 解析本地 ~/.care/settings.json（文件不存在时返回空默认配置，沿用 Pi 本机配置）
    settings = load_local_settings()
    # 显式传入 endpoint 时优先使用它；否则按 profile 名称或 settings.active_profile 解析
    resolved_profile = None if endpoint is not None else settings.resolve_profile(profile)
    effective_endpoint = (
        endpoint
        if endpoint is not None
        else (resolved_profile.to_endpoint() if resolved_profile is not None else None)
    )
    # 推理深度优先级：显式入参 > Profile 默认值 > None（由 Pi 默认决定）
    effective_thinking = (
        thinking_level
        if thinking_level is not None
        else (resolved_profile.thinking_level if resolved_profile is not None else None)
    )
    # Pi 可执行文件优先级：显式入参 > settings.json > 环境变量 PI_BIN > "pi"
    effective_pi_bin = pi_bin or settings.pi_bin or os.environ.get("PI_BIN", "pi")
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
                endpoint=effective_endpoint,
                thinking_level=effective_thinking,
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
    main()
