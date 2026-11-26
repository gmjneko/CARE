"""Runnable entry point to exercise CARE Workflow + Agent (Pi RPC) end-to-end."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Literal
from uuid import uuid4

from agent_runtime.adapters import (
    AdapterRegistry,
    ModelEndpoint,
    PiRpcAdapter,
    PiRpcConfig,
    ThinkingLevel,
)
from agent_runtime.config import checkpoints_db_path, load_local_settings
from agent_runtime.orchestrator import WorkflowRunner
from agent_runtime.protocol import (
    InteractionRequestEvent,
    InteractionResponse,
    NodeCompletedEvent,
    NodeStartedEvent,
    PendingInterrupt,
    RuntimeEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
    WorkflowResumedEvent,
    WorkflowStartedEvent,
)
from agent_runtime.workflows import create_default_workflow_registry

WorkflowName = Literal["single_agent", "plan_and_execute"]

_EXIT_COMMANDS = frozenset({"exit", "quit", "/exit", "/quit"})


def main() -> int:
    """IDE 直接右键运行入口，支持控制台连续多轮交互与审批确认。

    模型 / Base URL / API Key 默认读取 `~/.care/settings.json`（可通过 `CARE_HOME` 覆盖目录），
    未配置时沿用 Pi 本机默认配置；固定 `thread_id` 可跨进程续接同一会话或恢复未完成审批。
    """
    return run(
        # 初始提示词：None 时直接在控制台连续输入；填入文本则先执行首轮，随后继续控制台交互
        prompt=None,
        # 内置工作流: "single_agent" | "plan_and_execute"
        workflow="plan_and_execute",
        # Agent 工作目录（默认当前目录，支持相对路径、绝对路径或 ~）
        cwd=".",
        # 会话线程 ID：None 时本次会话自动生成并跨轮复用；设为固定值（如 "demo-1"）可跨进程续聊
        thread_id=None,
        # 使用 ~/.care/settings.json 中的配置名（None 表示使用 active_profile）
        profile=None,
        # 可选单次临时覆盖（与 profile 二选一；None 时沿用 profile 或 Pi 默认配置）
        endpoint=None,
        thinking_level=None,  # "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"
        # 运行时配置
        pi_bin=None,  # None 时依次读取 settings.json -> $PI_BIN -> "pi"
        # None 时默认持久化到 ~/.care/checkpoints.sqlite（也可填 ":memory:" 仅内存运行）
        checkpoint_db=None,
        json_output=False,  # True 时单次执行并输出原始 RuntimeEvent JSONL
    )


def run(
    prompt: str | None = None,
    *,
    workflow: WorkflowName | str = "single_agent",
    cwd: str | Path = ".",
    thread_id: str | None = None,
    profile: str | None = None,
    endpoint: ModelEndpoint | None = None,
    thinking_level: ThinkingLevel | None = None,
    pi_bin: str | None = None,
    checkpoint_db: str | Path | None = None,
    json_output: bool = False,
) -> int:
    """Run a CARE workflow interactively in the console (or single-shot when json_output=True)."""
    if prompt is not None and not prompt.strip():
        raise ValueError("prompt must be non-empty when provided")
    if json_output and prompt is None:
        raise ValueError("prompt is required when json_output=True")
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
    # Pi 可执行文件优先级：显式入参 > settings.json adapter_bins["pi"] > 环境变量 PI_BIN > "pi"
    effective_pi_bin = (
        pi_bin or settings.resolve_adapter_bin("pi") or os.environ.get("PI_BIN", "pi")
    )
    effective_checkpoint_db = checkpoint_db if checkpoint_db is not None else checkpoints_db_path()
    workspace_dir = Path(cwd).expanduser()
    effective_thread_id = thread_id or f"cli-{uuid4().hex[:12]}"

    async def _execute() -> int:
        workflow_registry = create_default_workflow_registry()
        adapter_registry = AdapterRegistry(
            {"pi": PiRpcAdapter(PiRpcConfig(command=(effective_pi_bin,)))}
        )

        render_state: dict[str, object] = {"stream": None, "saw_text": False, "is_agent": False}
        exit_code = 0
        pending_interrupts: tuple[PendingInterrupt, ...] = ()

        def _handle_event(event: RuntimeEvent) -> None:
            nonlocal pending_interrupts, exit_code
            if json_output:
                print(event.model_dump_json(), flush=True)
            else:
                _render_human(event, render_state)

            if isinstance(event.payload, WorkflowInterruptedEvent):
                pending_interrupts = event.payload.interrupts
            elif isinstance(event.payload, WorkflowCompletedEvent):
                exit_code = 0 if event.payload.status == "completed" else 1

        async def _console_interaction_handler(
            req: InteractionRequestEvent,
        ) -> InteractionResponse:
            response = _prompt_interaction(req, render_state)
            if response is None:
                return InteractionResponse(request_id=req.request_id, decision="cancel")
            return response

        async with WorkflowRunner(
            adapter_registry,
            workflow_registry,
            checkpoint_db=effective_checkpoint_db,
        ) as runner:
            if json_output:
                assert prompt is not None
                async for event in runner.run(
                    workflow,
                    prompt.strip(),
                    workspace_dir,
                    thread_id=effective_thread_id,
                    endpoint=effective_endpoint,
                    thinking_level=effective_thinking,
                ):
                    _handle_event(event)
                return exit_code

            print(
                f"[care] workflow={workflow} thread_id={effective_thread_id} "
                f"cwd={workspace_dir.resolve()}",
                flush=True,
            )
            print("提示: 输入消息开始连续对话，输入 exit/quit 或 Ctrl+C 退出。", flush=True)

            async def _resolve_interrupts() -> bool:
                nonlocal pending_interrupts
                while pending_interrupts:
                    intr = pending_interrupts[0]
                    resume_kwargs: dict[str, object] = {}
                    if intr.interaction is not None:
                        response = _prompt_interaction(intr.interaction, render_state)
                        if response is None:
                            return False
                        resume_kwargs["interaction_response"] = response
                    elif intr.kind == "workflow_interrupt":
                        _end_stream(render_state)
                        raw = _read_line(f"\n[{intr.node_id}] 请输入恢复值: ")
                        if raw is None:
                            return False
                        resume_kwargs["resume_value"] = raw.strip()
                    else:
                        _end_stream(render_state)
                        raw = _read_line(
                            f"\n[{intr.node_id}({intr.kind})] "
                            "回车恢复执行，输入 d 放弃本轮，或输入补充指令: "
                        )
                        if raw is None:
                            return False
                        text = raw.strip()
                        if text.lower() in ("d", "discard"):
                            await runner.discard(workflow, effective_thread_id)
                            pending_interrupts = ()
                            print("[care] 已放弃未完成轮次。", flush=True)
                            break
                        resume_kwargs["prompt"] = text or None

                    pending_interrupts = ()
                    async for event in runner.resume(
                        workflow,
                        effective_thread_id,
                        node_id=intr.node_id,
                        endpoint=effective_endpoint,
                        thinking_level=effective_thinking,
                        interaction_handler=_console_interaction_handler,
                        **resume_kwargs,
                    ):
                        _handle_event(event)
                return True

            try:
                if thread_id is not None:
                    try:
                        existing = await runner.get_thread_state(workflow, effective_thread_id)
                    except KeyError:
                        existing = None
                    if (
                        existing is not None
                        and existing.resumable
                        and existing.status in ("interrupted", "crashed")
                    ):
                        print(
                            f"[care] 检测到未完成的中断会话 (status={existing.status})，"
                            "先继续处理挂起项。",
                            flush=True,
                        )
                        pending_interrupts = existing.interrupts
                        if not await _resolve_interrupts():
                            return exit_code

                next_prompt: str | None = prompt.strip() if prompt is not None else None
                while True:
                    if next_prompt is None:
                        _end_stream(render_state)
                        raw = _read_line("\nYou > ")
                        if raw is None:
                            break
                        text = raw.strip()
                        if not text:
                            continue
                        if text.lower() in _EXIT_COMMANDS:
                            break
                        next_prompt = text

                    current_prompt = next_prompt
                    next_prompt = None
                    pending_interrupts = ()
                    async for event in runner.run(
                        workflow,
                        current_prompt,
                        workspace_dir,
                        thread_id=effective_thread_id,
                        endpoint=effective_endpoint,
                        thinking_level=effective_thinking,
                        interaction_handler=_console_interaction_handler,
                    ):
                        _handle_event(event)

                    if pending_interrupts and not await _resolve_interrupts():
                        break
            except KeyboardInterrupt:
                _end_stream(render_state)
                if pending_interrupts:
                    print(
                        f"\n[care] 已中断，断点已保存。下次可设置 "
                        f"thread_id={effective_thread_id!r} 继续恢复。",
                        flush=True,
                    )
                else:
                    print("\n[care] 已退出。", flush=True)

        return exit_code

    try:
        return asyncio.run(_execute())
    except KeyboardInterrupt:
        print("\n[care] 已退出。", flush=True)
        return 130


def _read_line(prompt_text: str) -> str | None:
    if sys.stdin is None or sys.stdin.closed:
        return None
    try:
        return input(prompt_text)
    except EOFError:
        return None


def _parse_approval_input(
    raw: str,
) -> tuple[Literal["approve", "deny", "cancel"], str | None] | None:
    text = raw.strip()
    if not text:
        return None
    head, _, rest = text.partition(" ")
    token = head.lower()
    feedback = rest.strip() or None
    if token in ("y", "yes"):
        return "approve", feedback
    if token in ("n", "no"):
        return "deny", feedback
    if token in ("c", "cancel"):
        return "cancel", feedback
    return None


def _prompt_interaction(
    req: InteractionRequestEvent,
    render_state: dict[str, object],
) -> InteractionResponse | None:
    _end_stream(render_state)
    title = f"[{req.title}] " if req.title else ""

    if req.kind == "approval":
        question = req.message or "是否继续执行"
        if req.tool_name:
            print(
                f"\n{title}{question} (tool={req.tool_name}, args={req.arguments})",
                flush=True,
            )
        else:
            print(f"\n{title}{question}", flush=True)
        while True:
            raw = _read_line("审批 [y(批准) / n(拒绝) / c(取消)，可空格附带说明]: ")
            if raw is None:
                return None
            parsed = _parse_approval_input(raw)
            if parsed is not None:
                decision, feedback = parsed
                return InteractionResponse(
                    request_id=req.request_id,
                    decision=decision,
                    feedback=feedback,
                )
            print("无效输入，请输入 y / n / c（可在后面加空格附带说明）。", flush=True)

    if req.kind == "user_input":
        question = req.message or "请输入回复"
        print(f"\n{title}{question}", flush=True)
        if req.options:
            for idx, option in enumerate(req.options, start=1):
                print(f"  {idx}. {option}", flush=True)
        while True:
            hint = (
                "请选择编号或输入内容（输入 c 取消）: "
                if req.options
                else "请输入回复（输入 c 取消）: "
            )
            raw = _read_line(hint)
            if raw is None:
                return None
            text = raw.strip()
            if not text:
                continue
            if text.lower() in ("c", "cancel"):
                return InteractionResponse(request_id=req.request_id, decision="cancel")
            if req.options and text.isdigit():
                choice = int(text)
                if 1 <= choice <= len(req.options):
                    text = req.options[choice - 1]
                else:
                    print(f"请输入 1-{len(req.options)} 之间的编号。", flush=True)
                    continue
            return InteractionResponse(request_id=req.request_id, decision="approve", value=text)

    return InteractionResponse(request_id=req.request_id, decision="approve")


def _end_stream(state: dict[str, object]) -> None:
    if state["stream"] is not None:
        print(flush=True)
        state["stream"] = None


def _render_human(event: RuntimeEvent, state: dict[str, object]) -> None:
    payload = event.payload

    if isinstance(payload, WorkflowStartedEvent):
        print(f"[workflow.started] workflow={event.workflow_id} run_id={event.run_id}", flush=True)
    elif isinstance(payload, WorkflowResumedEvent):
        print(
            f"[workflow.resumed] workflow={event.workflow_id} "
            f"thread_id={event.thread_id} from={payload.resumed_from}",
            flush=True,
        )
    elif isinstance(payload, NodeStartedEvent):
        _end_stream(state)
        state["saw_text"] = False
        state["is_agent"] = payload.adapter is not None
        adapter_suffix = f" adapter={payload.adapter}" if payload.adapter is not None else ""
        print(
            f"\n[node.started] node={payload.node_id} role={payload.role}{adapter_suffix}",
            flush=True,
        )
    elif isinstance(payload, ThinkingDeltaEvent):
        if state["stream"] != "thinking":
            _end_stream(state)
            print("[thinking] ", end="", flush=True)
            state["stream"] = "thinking"
        print(payload.delta, end="", flush=True)
    elif isinstance(payload, TextDeltaEvent):
        if state["stream"] != "text":
            _end_stream(state)
            state["stream"] = "text"
        state["saw_text"] = True
        print(payload.delta, end="", flush=True)
    elif isinstance(payload, ToolStartedEvent):
        _end_stream(state)
        print(f"[tool.started] {payload.tool_name}({payload.arguments})", flush=True)
    elif isinstance(payload, ToolOutputDeltaEvent):
        if state["stream"] != "tool":
            _end_stream(state)
            state["stream"] = "tool"
        print(payload.chunk, end="", flush=True)
    elif isinstance(payload, ToolCompletedEvent):
        _end_stream(state)
        status = "ERROR" if payload.is_error else "OK"
        duration = f" {payload.duration_ms}ms" if payload.duration_ms is not None else ""
        print(f"[tool.completed] {payload.tool_name} [{status}{duration}]", flush=True)
    elif isinstance(payload, NodeCompletedEvent):
        _end_stream(state)
        if state["is_agent"] and not state["saw_text"] and payload.summary:
            print(payload.summary, flush=True)
        state["saw_text"] = False
        state["is_agent"] = False
        usage = f" tokens={payload.usage.total_tokens}" if payload.usage else ""
        print(f"[node.completed] node={payload.node_id} status={payload.status}{usage}", flush=True)
        if payload.failure:
            print(
                f"  failure=({payload.failure.failure_class}) {payload.failure.message}",
                flush=True,
            )
    elif isinstance(payload, WorkflowInterruptedEvent):
        _end_stream(state)
        summary = ", ".join(f"{item.node_id}({item.kind})" for item in payload.interrupts)
        print(f"\n[workflow.interrupted] [{summary}] seq={event.seq}", flush=True)
    elif isinstance(payload, WorkflowCompletedEvent):
        _end_stream(state)
        print(f"\n[workflow.completed] status={payload.status} seq={event.seq}", flush=True)
        if payload.failure:
            print(
                f"  failure=({payload.failure.failure_class}) {payload.failure.message}",
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
