"""Production-path E2E scenarios for main.py interactive console and approval flow."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
MAIN_SCRIPT = RUNTIME_ROOT / "main.py"
FAKE_PI_SCRIPT = Path(__file__).resolve().parents[1] / "fakes" / "fake_pi_rpc.py"


def _create_pi_shim(tmp_path: Path, scenario: str) -> Path:
    """Create an executable shim invoking fake_pi_rpc.py over real stdio."""
    if os.name == "nt":
        shim = tmp_path / f"fake_pi_{scenario}.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{FAKE_PI_SCRIPT}" --scenario {scenario} %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = tmp_path / f"fake_pi_{scenario}.sh"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_PI_SCRIPT}" --scenario {scenario} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def _run_main_subprocess(
    *,
    tmp_path: Path,
    scenario: str,
    stdin_text: str,
    code: str | None = None,
) -> subprocess.CompletedProcess[str]:
    shim = _create_pi_shim(tmp_path, scenario)
    env = dict(
        os.environ,
        PI_BIN=str(shim),
        CARE_HOME=str(tmp_path / "care-home"),
        PYTHONUTF8="1",
    )
    cmd = (
        [sys.executable, str(MAIN_SCRIPT)]
        if code is None
        else [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(RUNTIME_ROOT)!r}); {code}",
        ]
    )
    return subprocess.run(
        cmd,
        input=stdin_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(tmp_path),
        env=env,
        timeout=30,
    )


def test_main_continuous_console_handles_plan_approval_and_multi_turn_over_non_tty_stdin(
    tmp_path: Path,
) -> None:
    """Gap: main.py previously skipped approval on non-TTY IDE consoles and exited after 1 turn."""
    # Subprocess stdin is a pipe (isatty() == False), matching IDE Run Consoles.
    # Turn 1: prompt -> planner -> invalid approval input -> approve with feedback -> executor.
    # Turn 2: follow-up prompt on same thread -> planner -> deny with feedback -> stays interactive.
    # Turn 3: exit cleanly.
    stdin_script = (
        "\n".join(
            [
                "Add cache layer",
                "maybe",
                "y Keep changes minimal",
                "Refactor config",
                "n Too broad",
                "exit",
            ]
        )
        + "\n"
    )

    proc = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="stateful_chat",
        stdin_text=stdin_script,
        code="import main; raise SystemExit(main.run(workflow='plan_and_execute'))",
    )

    assert proc.returncode == 1, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    stdout = proc.stdout

    # Turn 1 approved with feedback and executed on executor
    assert "无效输入" in stdout
    assert "[workflow.resumed] workflow=plan_and_execute" in stdout
    assert "[node.started] node=executor" in stdout
    assert "[workflow.completed] status=completed" in stdout

    # Parse JSON payloads emitted by stateful_chat nodes to verify both approval feedback
    # handoff and cross-turn planner session persistence inside one console process.
    node_payloads = [
        json.loads(line) for line in stdout.splitlines() if line.startswith('{"history":')
    ]
    assert len(node_payloads) == 3  # turn1 planner, turn1 executor, turn2 planner
    assert node_payloads[1]["turn_count"] == 2
    assert node_payloads[1]["session_file"] != node_payloads[0]["session_file"]
    assert "Currently in the **Plan** phase" in node_payloads[1]["history"][0]
    assert "## Approval Note\nKeep changes minimal" in node_payloads[1]["history"][-1]
    assert node_payloads[2]["turn_count"] == 2
    assert "Add cache layer" in node_payloads[2]["history"][0]
    assert "Refactor config" in node_payloads[2]["history"][1]

    # Turn 2 stayed in the same console session, reused planner session, and denied cleanly
    assert stdout.count("[workflow.started] workflow=plan_and_execute") == 2
    assert "[node.completed] node=plan_approval status=aborted" in stdout
    assert "[workflow.completed] status=aborted" in stdout

    # Normalize key lifecycle lines into a reproducible manifest artifact
    lifecycle = [
        line
        for line in stdout.splitlines()
        if line.startswith(
            (
                "[node.started]",
                "[node.completed]",
                "[workflow.interrupted]",
                "[workflow.completed]",
            )
        )
    ]
    manifest = {
        "turn_count": stdout.count("[workflow.started]"),
        "lifecycle": lifecycle,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    assert manifest["turn_count"] == 2
    assert "[node.started] node=executor role=executor adapter=pi" in lifecycle


def test_main_resumes_interrupted_thread_across_restarts_without_rerunning_planner(
    tmp_path: Path,
) -> None:
    """Exiting mid-approval keeps checkpoint; restarting with same thread_id resumes approval."""
    run_code = (
        "import main; "
        "raise SystemExit(main.run(prompt='Implement cache', workflow='plan_and_execute', "
        "thread_id='resume-cli-1'))"
    )
    # Process 1 hits EOF at approval prompt and stops cleanly with checkpoint paused.
    first = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="standard",
        stdin_text="",
        code=run_code,
    )
    assert first.returncode == 0, f"stderr={first.stderr}\nstdout={first.stdout}"
    assert "[workflow.interrupted] [plan_approval(workflow_interrupt)]" in first.stdout
    assert "[node.started] node=executor" not in first.stdout

    # Process 2 attaches to the same thread_id with prompt=None, approves pending plan, then exits.
    resume_code = (
        "import main; "
        "raise SystemExit(main.run(prompt=None, workflow='plan_and_execute', "
        "thread_id='resume-cli-1'))"
    )
    second = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="standard",
        stdin_text="y\nexit\n",
        code=resume_code,
    )
    assert second.returncode == 0, f"stderr={second.stderr}\nstdout={second.stdout}"
    assert "检测到未完成的中断会话" in second.stdout
    assert "[node.started] node=planner" not in second.stdout
    assert "[node.started] node=executor" in second.stdout
    assert "[workflow.completed] status=completed" in second.stdout


def test_main_handles_live_agent_blocking_interaction_and_json_mode(tmp_path: Path) -> None:
    """Live agent approval is answered in-place; json_output stays single-shot JSONL."""
    live_code = "import main; raise SystemExit(main.run(prompt='Deploy', workflow='single_agent'))"
    live = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="ui_confirm",
        stdin_text="y\nexit\n",
        code=live_code,
    )
    assert live.returncode == 0, f"stderr={live.stderr}\nstdout={live.stdout}"
    assert "UI_CONFIRM_OUTCOME=approved" in live.stdout

    json_code = (
        "import main; "
        "raise SystemExit(main.run(prompt='Hello', workflow='single_agent', json_output=True))"
    )
    jsonl = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="standard",
        stdin_text="",
        code=json_code,
    )
    assert jsonl.returncode == 0, f"stderr={jsonl.stderr}\nstdout={jsonl.stdout}"
    records = [json.loads(line) for line in jsonl.stdout.splitlines() if line.strip()]
    assert records[0]["payload"]["type"] == "workflow.started"
    assert records[-1]["payload"]["type"] == "workflow.completed"


def test_main_interactive_node_chat_then_next_over_non_tty_stdin(tmp_path: Path) -> None:
    """main.py console supports chatting inside an awaiting_input node and advancing via /next."""
    interactive_code = (
        "from langgraph.graph import END, START, StateGraph\n"
        "from agent_runtime.orchestrator import AgentNodeConfig, WorkflowState, build_agent_node\n"
        "import main\n"
        "def wf():\n"
        "    g = StateGraph(WorkflowState)\n"
        "    g.add_node('planner', build_agent_node(AgentNodeConfig("
        "id='planner', role='planner', tool_policy='read_only', "
        "append_system_prompt='You are the Planner agent.', "
        "prompt_builder=lambda s: s['prompt'], "
        "should_complete=lambda _o, _t: False)))\n"
        "    g.add_node('executor', build_agent_node(AgentNodeConfig("
        "id='executor', role='executor', "
        "prompt_builder=lambda s: f'EXEC:{s[\"node_outputs\"][\"planner\"].summary}')))\n"
        "    g.add_edge(START, 'planner')\n"
        "    g.add_edge('planner', 'executor')\n"
        "    g.add_edge('executor', END)\n"
        "    return g\n"
        "raise SystemExit(main.run(prompt='Draft v1', workflow=wf))"
    )
    proc = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="interactive_handoff",
        stdin_text="Refine v2\n/next\nexit\n",
        code=interactive_code,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    stdout = proc.stdout
    assert "[workflow.interrupted] [planner(awaiting_input)]" in stdout
    assert "[workflow.resumed] workflow=wf" in stdout
    assert "from=awaiting_input" in stdout
    assert "[node.completed] node=planner status=completed" in stdout
    assert "[node.started] node=executor role=executor adapter=pi" in stdout
    assert "[workflow.completed] status=completed" in stdout
    payloads = [json.loads(line) for line in stdout.splitlines() if line.startswith('{"history":')]
    assert len(payloads) == 3  # planner turn 1, planner turn 2, executor
    assert payloads[1]["history"] == ["Draft v1", "Refine v2"]
    assert "Refine v2" in payloads[2]["history"][0]


def test_main_run_validation_fails_fast() -> None:
    sys.path.insert(0, str(RUNTIME_ROOT))
    import main as main_module

    with pytest.raises(ValueError, match="prompt must be non-empty"):
        main_module.run("   ")
    with pytest.raises(ValueError, match="json_output=True"):
        main_module.run(None, json_output=True)
