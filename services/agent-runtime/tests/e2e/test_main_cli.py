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


def test_main_continuous_console_handles_interactive_plan_and_execute_over_non_tty_stdin(
    tmp_path: Path,
) -> None:
    """Default main.py runs interactive plan_and_execute: chat in planner, /next to executor
    (forking planner context), chat in executor without jumping back, /next to finish, then task 2.
    """
    stdin_script = (
        "\n".join(
            [
                "Add cache layer",
                "Keep changes minimal",
                "/next",
                "Run tests too",
                "/next",
                "Refactor config",
                "/discard",
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

    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    stdout = proc.stdout

    assert "[workflow.interrupted] [planner(awaiting_input)]" in stdout
    assert "[workflow.interrupted] [executor(awaiting_input)]" in stdout
    assert "[workflow.resumed] workflow=plan_and_execute" in stdout
    assert "[node.started] node=executor" in stdout
    assert "[workflow.completed] status=completed" in stdout
    assert "已放弃未完成轮次" in stdout

    # 0: planner turn 1, 1: planner turn 2, 2: executor turn 1 (forked planner's 2 turns!),
    # 3: executor turn 2 (in forked session), 4: task2 planner (continues clean planner session)
    node_payloads = [
        json.loads(line) for line in stdout.splitlines() if line.startswith('{"history":')
    ]
    assert len(node_payloads) == 5
    assert node_payloads[1]["turn_count"] == 2
    assert node_payloads[1]["history"][-1] == "Keep changes minimal"
    assert node_payloads[2]["session_file"] != node_payloads[1]["session_file"]
    assert node_payloads[2]["turn_count"] == 3
    assert "Currently in the **Plan** phase" in node_payloads[2]["history"][0]
    assert node_payloads[2]["history"][1] == "Keep changes minimal"
    assert "Currently in the **Execute** phase" in node_payloads[2]["history"][2]
    assert node_payloads[3]["session_file"] == node_payloads[2]["session_file"]
    assert node_payloads[3]["turn_count"] == 4
    assert node_payloads[3]["history"][-1] == "Run tests too"
    assert node_payloads[4]["session_file"] == node_payloads[0]["session_file"]
    assert node_payloads[4]["turn_count"] == 3
    assert "Refactor config" in node_payloads[4]["history"][-1]

    assert stdout.count("[workflow.started] workflow=plan_and_execute") == 2

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
    """Exiting mid-wait keeps checkpoint; restarting with same thread_id resumes in place."""
    run_code = (
        "import main\n"
        "raise SystemExit(main.run(prompt='Implement cache', workflow='plan_and_execute', "
        "thread_id='resume-cli-1'))"
    )
    # Process 1 exits cleanly while paused at planner(awaiting_input).
    first = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="standard",
        stdin_text="exit\n",
        code=run_code,
    )
    assert first.returncode == 0, f"stderr={first.stderr}\nstdout={first.stdout}"
    assert "[workflow.interrupted] [planner(awaiting_input)]" in first.stdout
    assert "[node.started] node=executor" not in first.stdout

    # Process 2 attaches to the same thread_id, advances planner -> executor, then finishes.
    resume_code = (
        "import main\n"
        "raise SystemExit(main.run(prompt=None, workflow='plan_and_execute', "
        "thread_id='resume-cli-1'))"
    )
    second = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="standard",
        stdin_text="/next\n/next\nexit\n",
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


def test_main_handles_workflow_approval_gate_over_non_tty_stdin(tmp_path: Path) -> None:
    """main.py console validates approval input and resumes workflow_interrupt approval gates."""
    approval_code = (
        "from langgraph.graph import END, START, StateGraph\n"
        "from agent_runtime.orchestrator import ("
        "AgentNodeConfig, WorkflowState, build_agent_node, build_approval_node, "
        "continue_if_running)\n"
        "import main\n"
        "def wf():\n"
        "    g = StateGraph(WorkflowState)\n"
        "    g.add_node('planner', build_agent_node(AgentNodeConfig("
        "id='planner', role='planner', tool_policy='read_only', "
        "append_system_prompt='You are the Planner agent.', "
        "prompt_builder=lambda s: s['prompt'])))\n"
        "    g.add_node('plan_approval', build_approval_node("
        "'plan_approval', title='确认执行计划', message='是否按照plan执行'))\n"
        "    g.add_node('executor', build_agent_node(AgentNodeConfig("
        "id='executor', role='executor', "
        "prompt_builder=lambda s: s['node_outputs']['plan_approval'].summary)))\n"
        "    g.add_edge(START, 'planner')\n"
        "    g.add_edge('planner', 'plan_approval')\n"
        "    g.add_conditional_edges('plan_approval', continue_if_running('executor'), "
        "{'executor': 'executor', END: END})\n"
        "    g.add_edge('executor', END)\n"
        "    return g\n"
        "raise SystemExit(main.run(prompt='Deploy v2', workflow=wf))"
    )
    proc = _run_main_subprocess(
        tmp_path=tmp_path,
        scenario="standard",
        stdin_text="maybe\ny Keep small\nexit\n",
        code=approval_code,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    assert "无效输入" in proc.stdout
    assert "[workflow.interrupted] [plan_approval(workflow_interrupt)]" in proc.stdout
    assert "[node.completed] node=plan_approval status=completed" in proc.stdout
    assert "[node.started] node=executor role=executor adapter=pi" in proc.stdout
    assert "[workflow.completed] status=completed" in proc.stdout


def test_main_run_validation_fails_fast() -> None:
    sys.path.insert(0, str(RUNTIME_ROOT))
    import main as main_module

    with pytest.raises(ValueError, match="prompt must be non-empty"):
        main_module.run("   ")
    with pytest.raises(ValueError, match="json_output=True"):
        main_module.run(None, json_output=True)
