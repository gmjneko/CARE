"""Production-path E2E tests for Step A (multi-turn sessions) and Step B (dual-layer recovery)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent_runtime.adapters import (
    AdapterCapabilityError,
    AdapterProtocolError,
    AdapterRegistry,
    AgentCapabilities,
    ModelEndpoint,
    PiRpcAdapter,
    PiRpcConfig,
)
from agent_runtime.orchestrator import (
    AgentNodeConfig,
    InflightNodeRecord,
    NodeRuntimeOverride,
    ResumeInput,
    SqlitePersistenceBackend,
    WorkflowRunner,
    WorkflowState,
    build_agent_node,
    build_approval_node,
    continue_if_running,
)
from agent_runtime.paths import sessions_dir
from agent_runtime.protocol import (
    InteractionRequestEvent,
    InteractionResponse,
    RuntimeEvent,
    TextDeltaEvent,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
    WorkflowResumedEvent,
)
from agent_runtime.workflows import create_default_workflow_registry

FAKE_PI_SCRIPT = Path(__file__).resolve().parents[1] / "fakes" / "fake_pi_rpc.py"


def _make_runner(scenario: str, *, checkpoint_db: str | Path) -> WorkflowRunner:
    adapter = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", scenario),
            request_timeout_s=10.0,
        )
    )
    return WorkflowRunner(
        AdapterRegistry({"pi": adapter}),
        create_default_workflow_registry(),
        persistence=SqlitePersistenceBackend(checkpoint_db),
    )


async def _hard_kill_worker(worker: asyncio.subprocess.Process) -> None:
    """Terminate the worker process tree so Windows venv trampolines release SQLite locks."""
    if worker.returncode is None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(worker.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            worker.kill()
    if worker.stdout is not None:
        await worker.stdout.read()
    await worker.wait()
    if os.name == "nt":
        # Windows unmaps crashed SQLite WAL -shm sections asynchronously after TerminateProcess.
        await asyncio.sleep(0.25)


def _write_manifest(run_dir: Path, name: str, events: list[RuntimeEvent]) -> dict[str, Any]:
    normalized = []
    for ev in events:
        payload = ev.payload.model_dump(mode="json")
        payload.pop("session_id", None)
        normalized.append(
            {
                "seq": ev.seq,
                "node_id": ev.node_id,
                "is_ephemeral": ev.is_ephemeral,
                "payload": payload,
            }
        )
    manifest = {
        "workflow_id": events[0].workflow_id if events else None,
        "event_count": len(normalized),
        "events": normalized,
    }
    (run_dir / f"{name}.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _build_three_node_workflow() -> StateGraph[WorkflowState]:
    """3-node workflow: planner -> executor -> reviewer (verifies downstream resume)."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                tool_policy="read_only",
                append_system_prompt="You are the Planner agent.",
                prompt_builder=lambda s: f"Plan: {s['prompt']}",
            )
        ),
    )
    graph.add_node(
        "executor",
        build_agent_node(
            AgentNodeConfig(
                id="executor",
                role="executor",
                prompt_builder=lambda s: f"Execute: {s['prompt']}",
            )
        ),
    )
    graph.add_node(
        "reviewer",
        build_agent_node(
            AgentNodeConfig(
                id="reviewer",
                role="reviewer",
                tool_policy="read_only",
                append_system_prompt="You are the Planner agent acting as read-only reviewer.",
                prompt_builder=lambda s: f"Review: {s['latest_output']}",
            )
        ),
    )
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", "reviewer")
    graph.add_edge("reviewer", END)
    return graph


async def test_single_agent_multi_turn_chat_persists_session_across_runner_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "conv-multi-turn"

    # Turn 1 in Runner #1
    turn1_events: list[RuntimeEvent] = []
    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner1:
        async for ev in runner1.run("single_agent", "First turn", tmp_path, thread_id=thread_id):
            turn1_events.append(ev)
        state1 = await runner1.get_thread_state("single_agent", thread_id)

    _write_manifest(tmp_path, "turn1", turn1_events)
    assert state1.status == "completed"
    assert state1.resumable is False
    session_ref_1 = state1.node_outputs["agent"].session_ref
    assert session_ref_1 is not None
    assert Path(session_ref_1).parent == sessions_dir()
    assert Path(session_ref_1).is_file()

    # Turn 2 in brand-new Runner #2 (simulating process restart)
    turn2_events: list[RuntimeEvent] = []
    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner2:
        async for ev in runner2.run("single_agent", "Second turn", tmp_path, thread_id=thread_id):
            turn2_events.append(ev)
        state2 = await runner2.get_thread_state("single_agent", thread_id)

        # Already completed thread rejects resume() clearly.
        with pytest.raises(ValueError, match="already completed"):
            async for _ in runner2.resume("single_agent", thread_id):
                pass

    _write_manifest(tmp_path, "turn2", turn2_events)
    final2 = turn2_events[-1].payload
    assert isinstance(final2, WorkflowCompletedEvent)
    payload2 = json.loads(final2.final_output)
    assert payload2["session_file"] == session_ref_1
    assert payload2["turn_count"] == 2
    assert payload2["history"] == ["First turn", "Second turn"]
    assert state2.node_outputs["agent"].session_ref == session_ref_1


async def test_plan_and_execute_isolates_session_refs_per_node(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "plan-exec-iso"

    async with _make_runner("standard", checkpoint_db=db_path) as runner:
        async for _ in runner.run("plan_and_execute", "Add cache", tmp_path, thread_id=thread_id):
            pass
        async for _ in runner.resume(
            "plan_and_execute", thread_id, resume_value={"decision": "approve"}
        ):
            pass
        state = await runner.get_thread_state("plan_and_execute", thread_id)

    planner_ref = state.node_outputs["planner"].session_ref
    executor_ref = state.node_outputs["executor"].session_ref
    assert planner_ref is not None
    assert executor_ref is not None
    assert planner_ref != executor_ref
    assert state.node_outputs["plan_approval"].session_ref is None
    assert state.node_outputs["plan_approval"].status == "completed"
    assert Path(planner_ref).parent == sessions_dir()
    assert Path(executor_ref).parent == sessions_dir()


async def test_live_hot_interaction_respond_in_place_without_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    events: list[RuntimeEvent] = []

    async with _make_runner("ui_question", checkpoint_db=db_path) as runner:

        async def _consume() -> None:
            async for ev in runner.run(
                "single_agent",
                "Pick cache",
                tmp_path,
                run_id="run-hot-1",
                interaction_mode="wait",
            ):
                events.append(ev)
                if isinstance(ev.payload, InteractionRequestEvent) and ev.payload.blocking:
                    live = await runner.get_thread_state("single_agent", ev.run_id)
                    assert live.status == "running" and not live.resumable
                    with pytest.raises(ValueError, match="already running"):
                        await anext(runner.resume("single_agent", ev.run_id))
                    with pytest.raises(ValueError, match="already running"):
                        await anext(
                            runner.run("single_agent", "other", tmp_path, thread_id=ev.run_id)
                        )
                    with pytest.raises(ValueError, match="already running"):
                        await runner.discard("single_agent", ev.run_id)
                    await runner.respond_interaction(
                        "run-hot-1",
                        InteractionResponse(
                            request_id=ev.payload.request_id,
                            decision="approve",
                            value="redis",
                        ),
                    )

        await asyncio.wait_for(_consume(), timeout=10.0)

    _write_manifest(tmp_path, "hot_interaction", events)
    assert not any(isinstance(e.payload, WorkflowInterruptedEvent) for e in events)
    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == "QUESTION_ANSWER=redis"


async def test_hot_interaction_pi_crash_degrades_to_resumable_interrupt_and_continues_downstream(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "three-node-pi-crash"

    # Run 1: planner succeeds, executor emits ask_question and Pi dies while waiting
    run1_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question_crash", checkpoint_db=db_path) as runner1:
        async for ev in runner1.run(
            _build_three_node_workflow,
            "Build cache layer",
            tmp_path,
            thread_id=thread_id,
            interaction_mode="wait",
        ):
            run1_events.append(ev)

    _write_manifest(tmp_path, "pi_crash_run1", run1_events)
    interrupted = run1_events[-1].payload
    assert isinstance(interrupted, WorkflowInterruptedEvent)
    assert len(interrupted.interrupts) == 1
    intr = interrupted.interrupts[0]
    assert intr.kind == "agent_interaction"
    assert intr.node_id == "executor"
    assert intr.interaction is not None
    assert intr.interaction.request_id == "ui-q-1"

    # Run 2 in fresh Runner (simulating recovery): planner skipped, executor resumes, reviewer runs!
    run2_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        thread_state = await runner2.get_thread_state(_build_three_node_workflow, thread_id)
        assert thread_state.status == "interrupted"
        assert thread_state.resumable is True
        assert len(thread_state.interrupts) == 1
        assert thread_state.interrupts[0].node_id == "executor"
        assert thread_state.interrupts[0].interaction is not None
        exec_session_ref = thread_state.node_outputs["executor"].session_ref
        assert exec_session_ref is not None

        async for ev in runner2.resume(
            _build_three_node_workflow,
            thread_id,
            interaction_response=InteractionResponse(
                request_id="ui-q-1",
                decision="approve",
                value="redis",
            ),
        ):
            run2_events.append(ev)

        final_state = await runner2.get_thread_state(_build_three_node_workflow, thread_id)

    _write_manifest(tmp_path, "pi_crash_run2", run2_events)
    assert isinstance(run2_events[0].payload, WorkflowResumedEvent)
    assert run2_events[0].payload.resumed_from == "agent_interaction"

    started_nodes = [ev.payload.node_id for ev in run2_events if ev.payload.type == "node.started"]
    assert started_nodes == ["executor", "reviewer"]

    final = run2_events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert "redis" in final_state.node_outputs["executor"].summary
    assert final_state.node_outputs["executor"].session_ref == exec_session_ref
    assert final_state.node_outputs["reviewer"].status == "completed"


async def test_suspend_all_during_hot_interaction_and_resume_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "suspend-all-thread"

    run1_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner1:
        async for _ in runner1.run(
            "plan_and_execute",
            "Choose backend",
            tmp_path,
            thread_id=thread_id,
            interaction_mode="wait",
        ):
            pass
        async for ev in runner1.resume(
            "plan_and_execute",
            thread_id,
            resume_value={"decision": "approve"},
            interaction_mode="wait",
        ):
            run1_events.append(ev)
            if isinstance(ev.payload, InteractionRequestEvent) and ev.payload.blocking:
                await runner1.suspend_all()

    assert isinstance(run1_events[-1].payload, WorkflowInterruptedEvent)
    assert len(run1_events[-1].payload.interrupts) == 1
    assert run1_events[-1].payload.interrupts[0].node_id == "executor"

    # Brand-new Runner simulates app restart
    run2_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert st.status == "interrupted"
        assert len(st.interrupts) == 1
        pending_req = st.interrupts[0].interaction
        assert pending_req is not None
        assert pending_req.title == "Select cache backend"

        async for ev in runner2.resume(
            "plan_and_execute",
            thread_id,
            interaction_response=InteractionResponse(
                request_id=pending_req.request_id,
                decision="approve",
                value="memory",
            ),
        ):
            run2_events.append(ev)

    started_nodes = [ev.payload.node_id for ev in run2_events if ev.payload.type == "node.started"]
    assert started_nodes == ["executor"]
    final = run2_events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert "memory" in final.final_output


async def test_immediate_suspend_mode_checkpoints_and_resumes_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "immediate-suspend"

    events1: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner:
        async for ev in runner.run(
            "single_agent",
            "Select backend",
            tmp_path,
            thread_id=thread_id,
            interaction_mode="suspend",
        ):
            events1.append(ev)

        assert isinstance(events1[-1].payload, WorkflowInterruptedEvent)
        events2: list[RuntimeEvent] = []
        async for ev in runner.resume(
            "single_agent",
            thread_id,
            interaction_response=InteractionResponse(
                request_id="ui-q-1",
                decision="approve",
                value="redis",
            ),
        ):
            events2.append(ev)

    final = events2[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert "redis" in final.final_output


async def test_executor_failure_resumes_skipping_completed_planner(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "fail-exec-resume"

    events1: list[RuntimeEvent] = []
    async with _make_runner("fail_executor_once", checkpoint_db=db_path) as runner1:
        async for _ in runner1.run(
            "plan_and_execute",
            "Refactor auth",
            tmp_path,
            thread_id=thread_id,
        ):
            pass
        async for ev in runner1.resume(
            "plan_and_execute",
            thread_id,
            resume_value={"decision": "approve"},
        ):
            events1.append(ev)

    final1 = events1[-1].payload
    assert isinstance(final1, WorkflowCompletedEvent)
    assert final1.status == "failed"

    events2: list[RuntimeEvent] = []
    async with _make_runner("fail_executor_once", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert st.status == "failed"
        assert st.resumable is True
        exec_ref = st.node_outputs["executor"].session_ref
        assert exec_ref is not None

        async for ev in runner2.resume(
            "plan_and_execute",
            thread_id,
            prompt="Retry after backoff",
        ):
            events2.append(ev)

    assert isinstance(events2[0].payload, WorkflowResumedEvent)
    assert events2[0].payload.resumed_from == "failure"
    started_nodes = [ev.payload.node_id for ev in events2 if ev.payload.type == "node.started"]
    assert started_nodes == ["executor"]
    final2 = events2[-1].payload
    assert isinstance(final2, WorkflowCompletedEvent)
    assert final2.status == "completed"
    assert final2.final_output == "EXECUTOR_RECOVERED(turns=2):Retry after backoff"


async def test_hard_kill_crash_recovers_session_and_pending_interaction_from_wal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "hard-kill-wal"

    async with _make_runner("standard", checkpoint_db=db_path) as first:
        async for _ in first.run("plan_and_execute", "First turn", tmp_path, thread_id=thread_id):
            pass
        async for _ in first.resume(
            "plan_and_execute", thread_id, resume_value={"decision": "approve"}
        ):
            pass

    # Kill the actual worker while its RPC child is waiting in the second turn.
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        str(tmp_path),
        thread_id,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert worker.stdout is not None
        request_id = (await asyncio.wait_for(worker.stdout.readline(), 15)).decode().strip()
        assert request_id == "ui-q-1"
    finally:
        await _hard_kill_worker(worker)

    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert st.status == "crashed"
        assert len(st.interrupts) == 1
        crash_req = st.interrupts[0].interaction
        assert crash_req is not None and crash_req.request_id == request_id
        ref = st.node_outputs["executor"].session_ref
        resumed_events = [
            ev
            async for ev in runner2.resume(
                "plan_and_execute",
                thread_id,
                interaction_response=InteractionResponse(
                    request_id=request_id, decision="approve", value="staging"
                ),
            )
        ]
        after = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert ref and after.node_outputs["executor"].session_ref == ref

    assert isinstance(resumed_events[0].payload, WorkflowResumedEvent)
    assert resumed_events[0].payload.resumed_from == "crash"
    started_nodes = [
        ev.payload.node_id for ev in resumed_events if ev.payload.type == "node.started"
    ]
    assert started_nodes == ["executor"]
    final = resumed_events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert "staging" in final.final_output
    assert after.status == "completed"
    assert after.inflight_nodes == ()


async def test_workflow_level_langgraph_interrupt_resumes_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "langgraph-interrupt-gate"

    run1_events: list[RuntimeEvent] = []
    async with _make_runner("standard", checkpoint_db=db_path) as runner1:
        async for ev in runner1.run(
            "plan_and_execute",
            "Ship v2",
            tmp_path,
            thread_id=thread_id,
        ):
            run1_events.append(ev)

    interrupted = run1_events[-1].payload
    assert isinstance(interrupted, WorkflowInterruptedEvent)
    assert len(interrupted.interrupts) == 1
    gate_intr = interrupted.interrupts[0]
    assert gate_intr.kind == "workflow_interrupt"
    assert gate_intr.node_id == "plan_approval"
    assert gate_intr.interaction is not None
    assert gate_intr.interaction.kind == "approval"
    assert gate_intr.interaction.message == "是否按照plan执行"
    assert "[PLAN tools=default]" in gate_intr.payload["content"]

    # Brand-new Runner resumes across process restart without re-running planner
    run2_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert st.status == "interrupted"
        assert st.resumable is True
        assert len(st.interrupts) == 1
        assert st.interrupts[0].node_id == "plan_approval"

        async for ev in runner2.resume(
            "plan_and_execute",
            thread_id,
            resume_value={"decision": "approve"},
            interaction_mode="suspend",
        ):
            run2_events.append(ev)

    pause = run2_events[-1].payload
    assert isinstance(pause, WorkflowInterruptedEvent) and len(pause.interrupts) == 1
    pause_req = pause.interrupts[0].interaction
    assert pause_req is not None
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner3:
        with pytest.raises(ValueError, match="request_id"):
            async for _ in runner3.resume(
                "plan_and_execute",
                thread_id,
                interaction_response=InteractionResponse(request_id="stale", decision="deny"),
            ):
                pass
        run3_events = [
            ev
            async for ev in runner3.resume(
                "plan_and_execute",
                thread_id,
                interaction_response=InteractionResponse(
                    request_id=pause_req.request_id, decision="approve", value="redis"
                ),
            )
        ]

    assert isinstance(run2_events[0].payload, WorkflowResumedEvent)
    assert run2_events[0].payload.resumed_from == "workflow_interrupt"
    started_nodes = [ev.payload.node_id for ev in run2_events if ev.payload.type == "node.started"]
    assert started_nodes == ["plan_approval", "executor"]
    final = run3_events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert "redis" in final.final_output


async def test_committed_graph_stays_complete_if_worker_dies_before_stream_cleanup(
    tmp_path: Path,
) -> None:
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        str(tmp_path),
        "committed",
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert worker.stdout is not None
        final_output = (await asyncio.wait_for(worker.stdout.readline(), 15)).decode().strip()
        assert final_output
    finally:
        await _hard_kill_worker(worker)
    db_path = tmp_path / "checkpoints.sqlite"
    async with _make_runner("standard", checkpoint_db=db_path) as runner:
        with sqlite3.connect(db_path) as conn:
            before_rows = conn.execute(
                "SELECT COUNT(*) FROM care_inflight_nodes WHERE thread_id = ?", ("committed",)
            ).fetchone()[0]
        assert before_rows == 1

        state = await runner.get_thread_state("single_agent", "committed")
        assert state.status == "completed" and not state.resumable
        assert state.node_outputs["agent"].summary == final_output
        assert not state.inflight_nodes

        # Read-only get_thread_state() must not mutate SQLite.
        with sqlite3.connect(db_path) as conn:
            after_read_rows = conn.execute(
                "SELECT COUNT(*) FROM care_inflight_nodes WHERE thread_id = ?", ("committed",)
            ).fetchone()[0]
        assert after_read_rows == 1

        with pytest.raises(ValueError, match="already completed"):
            await anext(runner.resume("single_agent", "committed"))

        # Subsequent write operation cleans up the stale WAL row.
        async for _ in runner.run("single_agent", "Follow up", tmp_path, thread_id="committed"):
            pass
        with sqlite3.connect(db_path) as conn:
            after_write_rows = conn.execute(
                "SELECT COUNT(*) FROM care_inflight_nodes WHERE thread_id = ?", ("committed",)
            ).fetchone()[0]
        assert after_write_rows == 0


async def test_aborted_turn_allows_fresh_run_preserving_session_history(tmp_path: Path) -> None:
    """Gap: aborting an active node previously left snapshot.next non-empty and blocked run()."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "abort-then-new-run"

    turn1_events: list[RuntimeEvent] = []
    async with _make_runner("abortable", checkpoint_db=db_path) as runner1:
        async for ev in runner1.run(
            "single_agent",
            "Initial aborted request",
            tmp_path,
            run_id="run-abort-new-1",
            thread_id=thread_id,
        ):
            turn1_events.append(ev)
            if isinstance(ev.payload, TextDeltaEvent):
                await runner1.abort("run-abort-new-1")

        st1 = await runner1.get_thread_state("single_agent", thread_id)

    assert isinstance(turn1_events[-1].payload, WorkflowCompletedEvent)
    assert turn1_events[-1].payload.status == "aborted"
    assert st1.status == "aborted"
    assert st1.resumable is True
    session_ref_1 = st1.node_outputs["agent"].session_ref
    assert session_ref_1 is not None

    # User sends a brand-new message directly via run() instead of resume()
    turn2_events: list[RuntimeEvent] = []
    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner2:
        async for ev in runner2.run(
            "single_agent",
            "Never mind, solve B instead",
            tmp_path,
            thread_id=thread_id,
        ):
            turn2_events.append(ev)
        st2 = await runner2.get_thread_state("single_agent", thread_id)

    _write_manifest(tmp_path, "abort_then_new_run", turn2_events)
    final2 = turn2_events[-1].payload
    assert isinstance(final2, WorkflowCompletedEvent)
    assert final2.status == "completed"
    payload2 = json.loads(final2.final_output)
    assert payload2["session_file"] == session_ref_1
    assert payload2["history"] == ["Initial aborted request", "Never mind, solve B instead"]
    assert st2.status == "completed"
    assert st2.resumable is False
    assert st2.node_outputs["agent"].session_ref == session_ref_1


async def test_failed_multinode_turn_allows_fresh_run_preserving_all_node_sessions(
    tmp_path: Path,
) -> None:
    """Gap: failed executor paused inside subgraph had not flushed its session_ref to root state."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "fail-then-new-run"

    async with _make_runner("fail_executor_once", checkpoint_db=db_path) as runner1:
        async for _ in runner1.run(
            "plan_and_execute", "Original task", tmp_path, thread_id=thread_id
        ):
            pass
        events1 = [
            ev
            async for ev in runner1.resume(
                "plan_and_execute", thread_id, resume_value={"decision": "approve"}
            )
        ]
        st1 = await runner1.get_thread_state("plan_and_execute", thread_id)

    assert isinstance(events1[-1].payload, WorkflowCompletedEvent)
    assert events1[-1].payload.status == "failed"
    assert st1.status == "failed" and st1.resumable is True
    plan_ref = st1.node_outputs["planner"].session_ref
    exec_ref = st1.node_outputs["executor"].session_ref
    assert plan_ref is not None and exec_ref is not None

    async with _make_runner("standard", checkpoint_db=db_path) as runner2:
        events2_plan = [
            ev
            async for ev in runner2.run(
                "plan_and_execute", "Fresh replacement task", tmp_path, thread_id=thread_id
            )
        ]
        assert isinstance(events2_plan[-1].payload, WorkflowInterruptedEvent)
        events2_exec = [
            ev
            async for ev in runner2.resume(
                "plan_and_execute", thread_id, resume_value={"decision": "approve"}
            )
        ]
        events2 = [*events2_plan, *events2_exec]
        st2 = await runner2.get_thread_state("plan_and_execute", thread_id)

    _write_manifest(tmp_path, "fail_then_new_run", events2)
    started_plan = [ev.payload.node_id for ev in events2_plan if ev.payload.type == "node.started"]
    started_exec = [ev.payload.node_id for ev in events2_exec if ev.payload.type == "node.started"]
    assert started_plan == ["planner", "plan_approval"]
    assert started_exec == ["plan_approval", "executor"]
    assert isinstance(events2[-1].payload, WorkflowCompletedEvent)
    assert events2[-1].payload.status == "completed"
    assert st2.status == "completed" and st2.resumable is False
    assert st2.node_outputs["planner"].session_ref == plan_ref
    assert st2.node_outputs["executor"].session_ref is not None
    assert st2.node_outputs["executor"].session_ref not in (plan_ref, exec_ref)


async def test_unresolved_interrupt_and_crash_block_run_until_discarded(tmp_path: Path) -> None:
    """Gap: unresolved pauses must reject run() until explicitly discarded, keeping session_ref."""
    db_path = tmp_path / "checkpoints.sqlite"

    # Case 1: Cold agent interaction pause (interrupted)
    thread_int = "discard-interrupted"
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner1:
        async for _ in runner1.run(
            "plan_and_execute",
            "Pick backend",
            tmp_path,
            thread_id=thread_int,
            interaction_mode="suspend",
        ):
            pass
        events1 = [
            ev
            async for ev in runner1.resume(
                "plan_and_execute",
                thread_int,
                resume_value={"decision": "approve"},
                interaction_mode="suspend",
            )
        ]
        st_paused = await runner1.get_thread_state("plan_and_execute", thread_int)
        assert isinstance(events1[-1].payload, WorkflowInterruptedEvent)
        assert st_paused.status == "interrupted" and st_paused.resumable is True
        plan_ref = st_paused.node_outputs["planner"].session_ref
        exec_ref = st_paused.node_outputs["executor"].session_ref
        assert plan_ref is not None and exec_ref is not None

        with pytest.raises(ValueError, match="interrupted"):
            await anext(
                runner1.run("plan_and_execute", "Overwrite pause", tmp_path, thread_id=thread_int)
            )

        discarded = await runner1.discard("plan_and_execute", thread_int)
        assert discarded.status == "aborted"
        assert discarded.resumable is False
        assert discarded.next_nodes == ()
        assert discarded.interrupts == ()
        assert discarded.node_outputs["planner"].session_ref == plan_ref
        assert discarded.node_outputs["executor"].session_ref == exec_ref

        # Idempotent on already-closed thread, and resume() now rejects it
        assert await runner1.discard("plan_and_execute", thread_int) == discarded
        with pytest.raises(ValueError, match="already aborted"):
            await anext(runner1.resume("plan_and_execute", thread_int))

    async with _make_runner("standard", checkpoint_db=db_path) as runner2:
        async for _ in runner2.run(
            "plan_and_execute", "Start fresh after discard", tmp_path, thread_id=thread_int
        ):
            pass
        events_after = [
            ev
            async for ev in runner2.resume(
                "plan_and_execute", thread_int, resume_value={"decision": "approve"}
            )
        ]
        st_after = await runner2.get_thread_state("plan_and_execute", thread_int)

    _write_manifest(tmp_path, "discard_interrupted_then_run", events_after)
    assert isinstance(events_after[-1].payload, WorkflowCompletedEvent)
    assert events_after[-1].payload.status == "completed"
    assert st_after.node_outputs["planner"].session_ref == plan_ref
    assert st_after.node_outputs["executor"].session_ref is not None
    assert st_after.node_outputs["executor"].session_ref not in (plan_ref, exec_ref)

    # Case 2: Hard-killed worker crash (crashed with live WAL row)
    thread_crash = "discard-crashed"
    async with _make_runner("standard", checkpoint_db=db_path) as first:
        async for _ in first.run("plan_and_execute", "Turn 1", tmp_path, thread_id=thread_crash):
            pass
        async for _ in first.resume(
            "plan_and_execute", thread_crash, resume_value={"decision": "approve"}
        ):
            pass

    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        str(tmp_path),
        thread_crash,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert worker.stdout is not None
        req_id = (await asyncio.wait_for(worker.stdout.readline(), 15)).decode().strip()
        assert req_id == "ui-q-1"
    finally:
        await _hard_kill_worker(worker)

    async with _make_runner("standard", checkpoint_db=db_path) as runner3:
        st_crash = await runner3.get_thread_state("plan_and_execute", thread_crash)
        assert st_crash.status == "crashed" and st_crash.resumable is True
        crash_exec_ref = st_crash.node_outputs["executor"].session_ref
        assert crash_exec_ref is not None

        with pytest.raises(ValueError, match="crashed"):
            await anext(
                runner3.run("plan_and_execute", "Overwrite crash", tmp_path, thread_id=thread_crash)
            )

        closed_crash = await runner3.discard("plan_and_execute", thread_crash)
        assert closed_crash.status == "aborted"
        assert closed_crash.resumable is False
        assert closed_crash.inflight_nodes == ()
        assert closed_crash.node_outputs["executor"].session_ref == crash_exec_ref

        async for _ in runner3.run(
            "plan_and_execute",
            "Fresh turn after crash discard",
            tmp_path,
            thread_id=thread_crash,
        ):
            pass
        recovered_events = [
            ev
            async for ev in runner3.resume(
                "plan_and_execute", thread_crash, resume_value={"decision": "approve"}
            )
        ]
        st_recovered = await runner3.get_thread_state("plan_and_execute", thread_crash)

    _write_manifest(tmp_path, "discard_crashed_then_run", recovered_events)
    assert isinstance(recovered_events[-1].payload, WorkflowCompletedEvent)
    assert recovered_events[-1].payload.status == "completed"
    assert st_recovered.node_outputs["executor"].session_ref is not None
    assert st_recovered.node_outputs["executor"].session_ref != crash_exec_ref
    assert (
        st_recovered.node_outputs["executor"].session_ref
        != st_recovered.node_outputs["planner"].session_ref
    )

    # Case 3: Workflow-level LangGraph interrupt() gate (paused is None, no WAL)
    def _build_gate_workflow() -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "planner",
            build_agent_node(
                AgentNodeConfig(
                    id="planner",
                    role="planner",
                    tool_policy="read_only",
                    append_system_prompt="You are the Planner agent.",
                    prompt_builder=lambda s: f"Plan: {s['prompt']}",
                )
            ),
        )
        graph.add_node(
            "gate", lambda s: {"status": "running" if interrupt({"p": 1}) else "aborted"}
        )
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "gate")
        graph.add_edge("gate", END)
        return graph

    thread_gate = "discard-workflow-gate"
    async with _make_runner("standard", checkpoint_db=db_path) as runner4:
        gate_events = [
            ev
            async for ev in runner4.run(
                _build_gate_workflow, "Need approval", tmp_path, thread_id=thread_gate
            )
        ]
        assert isinstance(gate_events[-1].payload, WorkflowInterruptedEvent)
        assert len(gate_events[-1].payload.interrupts) == 1
        assert gate_events[-1].payload.interrupts[0].kind == "workflow_interrupt"
        st_gate = await runner4.get_thread_state(_build_gate_workflow, thread_gate)
        gate_plan_ref = st_gate.node_outputs["planner"].session_ref
        assert gate_plan_ref is not None

        with pytest.raises(ValueError, match="interrupted"):
            await anext(
                runner4.run(_build_gate_workflow, "Skip gate", tmp_path, thread_id=thread_gate)
            )

        closed_gate = await runner4.discard(_build_gate_workflow, thread_gate)
        assert closed_gate.status == "aborted"
        assert closed_gate.resumable is False
        assert closed_gate.next_nodes == ()
        assert closed_gate.interrupts == ()
        assert closed_gate.node_outputs["planner"].session_ref == gate_plan_ref

        gate_restarted = [
            ev
            async for ev in runner4.run(
                _build_gate_workflow, "New plan after discard", tmp_path, thread_id=thread_gate
            )
        ]
        assert isinstance(gate_restarted[-1].payload, WorkflowInterruptedEvent)
        st_gate2 = await runner4.get_thread_state(_build_gate_workflow, thread_gate)
        assert st_gate2.node_outputs["planner"].session_ref == gate_plan_ref


async def test_first_turn_crash_preserves_configured_role_and_wal_less_crash_rejects_prompt(
    tmp_path: Path,
) -> None:
    """Gaps: Turn-1 WAL crash lost role; WAL-less crash silently dropped continuation prompt."""
    db_path = tmp_path / "checkpoints.sqlite"

    # 1. Turn-1 crash on single_agent (node_id="agent", role="general") preserves role="general"
    async with _make_runner("malformed_stdout", checkpoint_db=db_path) as runner1:
        with pytest.raises(AdapterProtocolError):
            async for _ in runner1.run(
                "single_agent", "Crash turn 1", tmp_path, thread_id="crash-role-t1"
            ):
                pass
        st_role = await runner1.get_thread_state("single_agent", "crash-role-t1")

    assert st_role.status == "crashed"
    assert st_role.node_outputs["agent"].role == "general"
    assert len(st_role.inflight_nodes) == 1
    assert st_role.inflight_nodes[0].role == "general"

    # 2. WAL-less crash (fails before any agent turn starts) rejects prompt and replays cleanly
    attempts = 0

    def _build_wal_less_crash_workflow() -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "planner",
            build_agent_node(
                AgentNodeConfig(
                    id="planner",
                    role="planner",
                    tool_policy="read_only",
                    append_system_prompt="You are the Planner agent.",
                    prompt_builder=lambda s: f"Plan: {s['prompt']}",
                )
            ),
        )

        def _flaky_step(state: WorkflowState) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient non-agent node crash")
            return {"latest_output": f"REPLAYED:{state['latest_output']}"}

        graph.add_node("post_step", _flaky_step)
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "post_step")
        graph.add_edge("post_step", END)
        return graph

    thread_wal_less = "crash-no-wal"
    async with _make_runner("standard", checkpoint_db=db_path) as runner2:
        with pytest.raises(RuntimeError, match="transient non-agent node crash"):
            async for _ in runner2.run(
                _build_wal_less_crash_workflow,
                "Run post step",
                tmp_path,
                thread_id=thread_wal_less,
            ):
                pass

        st_no_wal = await runner2.get_thread_state(_build_wal_less_crash_workflow, thread_wal_less)
        assert st_no_wal.status == "crashed"
        assert st_no_wal.inflight_nodes == ()
        assert st_no_wal.next_nodes == ("post_step",)

        with pytest.raises(ValueError, match="continuation prompt"):
            await anext(
                runner2.resume(
                    _build_wal_less_crash_workflow,
                    thread_wal_less,
                    prompt="Should not be silently dropped",
                )
            )

        replayed_events = [
            ev async for ev in runner2.resume(_build_wal_less_crash_workflow, thread_wal_less)
        ]

    _write_manifest(tmp_path, "wal_less_crash_replay", replayed_events)
    assert isinstance(replayed_events[0].payload, WorkflowResumedEvent)
    assert replayed_events[0].payload.resumed_from == "crash"
    started = [ev.payload.node_id for ev in replayed_events if ev.payload.type == "node.started"]
    assert started == []
    final = replayed_events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output.startswith("REPLAYED:[PLAN ")


def _build_parallel_join_workflow() -> StateGraph[WorkflowState]:
    """Fan-out / fan-in workflow: START -> (worker_a, worker_b) -> aggregator -> END."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "worker_a",
        build_agent_node(
            AgentNodeConfig(
                id="worker_a",
                role="researcher_a",
                prompt_builder=lambda s: f"Branch A: {s['prompt']}",
            )
        ),
    )
    graph.add_node(
        "worker_b",
        build_agent_node(
            AgentNodeConfig(
                id="worker_b",
                role="researcher_b",
                prompt_builder=lambda s: f"Branch B: {s['prompt']}",
            )
        ),
    )

    def _aggregator(state: WorkflowState) -> dict[str, Any]:
        out_a = state["node_outputs"]["worker_a"].summary
        out_b = state["node_outputs"]["worker_b"].summary
        return {"latest_output": f"JOIN[a={out_a} | b={out_b}]"}

    graph.add_node("aggregator", _aggregator)
    graph.add_edge(START, "worker_a")
    graph.add_edge(START, "worker_b")
    graph.add_edge("worker_a", "aggregator")
    graph.add_edge("worker_b", "aggregator")
    graph.add_edge("aggregator", END)
    return graph


async def test_parallel_agents_complete_and_live_hot_interactions_stay_isolated(
    tmp_path: Path,
) -> None:
    """Gap: parallel agents collided on LastValue channels and single-entry RunContext dicts."""
    db_path = tmp_path / "checkpoints.sqlite"

    # 1. Parallel completion in the same superstep without InvalidUpdateError
    async with _make_runner("standard", checkpoint_db=db_path) as runner1:
        events1 = [
            ev
            async for ev in runner1.run(
                _build_parallel_join_workflow, "Parallel task", tmp_path, thread_id="par-ok"
            )
        ]
        st1 = await runner1.get_thread_state(_build_parallel_join_workflow, "par-ok")

    _write_manifest(tmp_path, "parallel_complete", events1)
    assert isinstance(events1[-1].payload, WorkflowCompletedEvent)
    assert events1[-1].payload.status == "completed"
    assert events1[-1].payload.final_output.startswith("JOIN[a=[EXEC ")
    ref_a = st1.node_outputs["worker_a"].session_ref
    ref_b = st1.node_outputs["worker_b"].session_ref
    assert ref_a is not None and ref_b is not None and ref_a != ref_b

    # 2. Concurrent live hot interactions (unique request_ids) + steer ambiguity guard
    seen_requests: dict[str, str] = {}
    hot_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question_unique", checkpoint_db=db_path) as runner2:

        async def _consume() -> None:
            async for ev in runner2.run(
                _build_parallel_join_workflow,
                "Pick two caches",
                tmp_path,
                run_id="run-par-hot",
                thread_id="par-hot",
                interaction_mode="wait",
            ):
                hot_events.append(ev)
                if isinstance(ev.payload, InteractionRequestEvent) and ev.payload.blocking:
                    assert ev.node_id is not None
                    seen_requests[ev.node_id] = ev.payload.request_id
                    if len(seen_requests) == 2:
                        with pytest.raises(ValueError, match="Multiple active agent sessions"):
                            await runner2.steer("run-par-hot", "ambiguous steer")
                        await runner2.respond_interaction(
                            "run-par-hot",
                            InteractionResponse(
                                request_id=seen_requests["worker_a"],
                                decision="approve",
                                value="redis",
                            ),
                        )
                        await runner2.respond_interaction(
                            "run-par-hot",
                            InteractionResponse(
                                request_id=seen_requests["worker_b"],
                                decision="approve",
                                value="memory",
                            ),
                        )

        await asyncio.wait_for(_consume(), timeout=15.0)

    _write_manifest(tmp_path, "parallel_hot_interactions", hot_events)
    final_hot = hot_events[-1].payload
    assert isinstance(final_hot, WorkflowCompletedEvent)
    assert final_hot.status == "completed"
    assert "a=QUESTION_ANSWER=redis" in final_hot.final_output
    assert "b=QUESTION_ANSWER=memory" in final_hot.final_output


async def test_parallel_cold_interrupts_progressive_and_batch_concurrent_resume(
    tmp_path: Path,
) -> None:
    """Gap: parallel cold interrupts dropped all but one branch and lacked batch resume."""
    db_path = tmp_path / "checkpoints.sqlite"

    # Part 1: Progressive single-branch resume via auto-matched request_id (zero re-run of sibling)
    tid_prog = "par-cold-progressive"
    async with _make_runner("ui_question_unique", checkpoint_db=db_path) as runner1:
        pause_events = [
            ev
            async for ev in runner1.run(
                _build_parallel_join_workflow,
                "Choose caches",
                tmp_path,
                thread_id=tid_prog,
                interaction_mode="suspend",
            )
        ]
        st_both = await runner1.get_thread_state(_build_parallel_join_workflow, tid_prog)

    assert pause_events[-1].node_id is None
    assert isinstance(pause_events[-1].payload, WorkflowInterruptedEvent)
    assert {i.node_id for i in pause_events[-1].payload.interrupts} == {"worker_a", "worker_b"}
    by_node = {i.node_id: i for i in st_both.interrupts}
    req_a = by_node["worker_a"].interaction
    req_b = by_node["worker_b"].interaction
    assert req_a is not None and req_b is not None and req_a.request_id != req_b.request_id

    async with _make_runner("ui_question_unique", checkpoint_db=db_path) as runner2:
        with pytest.raises(ValueError, match="Multiple nodes are paused"):
            await anext(runner2.resume(_build_parallel_join_workflow, tid_prog))

        # Resume ONLY worker_a; worker_b must stay paused without re-running Pi.
        step_a_events = [
            ev
            async for ev in runner2.resume(
                _build_parallel_join_workflow,
                tid_prog,
                interaction_response=InteractionResponse(
                    request_id=req_a.request_id, decision="approve", value="redis"
                ),
            )
        ]
        started_a = [
            ev.payload.node_id for ev in step_a_events if ev.payload.type == "node.started"
        ]
        assert started_a == ["worker_a"]
        assert isinstance(step_a_events[-1].payload, WorkflowInterruptedEvent)
        assert [i.node_id for i in step_a_events[-1].payload.interrupts] == ["worker_b"]

        # Resume worker_b; aggregator automatically runs and joins both branches.
        step_b_events = [
            ev
            async for ev in runner2.resume(
                _build_parallel_join_workflow,
                tid_prog,
                interaction_response=InteractionResponse(
                    request_id=req_b.request_id, decision="approve", value="memory"
                ),
            )
        ]

    _write_manifest(tmp_path, "parallel_progressive_resume_b", step_b_events)
    started_b = [ev.payload.node_id for ev in step_b_events if ev.payload.type == "node.started"]
    assert started_b == ["worker_b"]
    final_prog = step_b_events[-1].payload
    assert isinstance(final_prog, WorkflowCompletedEvent)
    assert final_prog.status == "completed"
    assert "redis" in final_prog.final_output and "memory" in final_prog.final_output

    # Part 2: Batch concurrent resume via resumes={...} (including duplicate request_id guard)
    tid_batch = "par-cold-batch"
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner3:
        async for _ in runner3.run(
            _build_parallel_join_workflow,
            "Batch choose",
            tmp_path,
            thread_id=tid_batch,
            interaction_mode="suspend",
        ):
            pass
        # Both fake subprocesses emitted identical request_id "ui-q-1" -> must require node_id
        with pytest.raises(ValueError, match="share request_id"):
            await anext(
                runner3.resume(
                    _build_parallel_join_workflow,
                    tid_batch,
                    interaction_response=InteractionResponse(
                        request_id="ui-q-1", decision="approve", value="redis"
                    ),
                )
            )
        with pytest.raises(ValueError, match="Cannot combine"):
            await anext(
                runner3.resume(
                    _build_parallel_join_workflow,
                    tid_batch,
                    prompt="extra",
                    resumes={"worker_a": ResumeInput(prompt="x")},
                )
            )

        batch_events = [
            ev
            async for ev in runner3.resume(
                _build_parallel_join_workflow,
                tid_batch,
                resumes={
                    "worker_a": ResumeInput(
                        interaction_response=InteractionResponse(
                            request_id="ui-q-1", decision="approve", value="redis"
                        )
                    ),
                    "worker_b": ResumeInput(
                        interaction_response=InteractionResponse(
                            request_id="ui-q-1", decision="approve", value="memory"
                        )
                    ),
                },
            )
        ]

    _write_manifest(tmp_path, "parallel_batch_resume", batch_events)
    batch_started = {ev.payload.node_id for ev in batch_events if ev.payload.type == "node.started"}
    assert batch_started == {"worker_a", "worker_b"}
    final_batch = batch_events[-1].payload
    assert isinstance(final_batch, WorkflowCompletedEvent)
    assert final_batch.status == "completed"
    assert "redis" in final_batch.final_output and "memory" in final_batch.final_output


async def test_parallel_mixed_interrupted_and_failed_priority_and_dual_failure_supersede(
    tmp_path: Path,
) -> None:
    """Gap: mixed parallel states must prioritize interrupted over failed and preserve all refs."""
    db_path = tmp_path / "checkpoints.sqlite"

    adapter_q = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "ui_question_unique"),
            request_timeout_s=10.0,
        )
    )
    adapter_fail = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "fail_executor_once"),
            request_timeout_s=10.0,
        )
    )
    adapter_ok = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "standard"),
            request_timeout_s=10.0,
        )
    )

    mixed_overrides = {
        "worker_a": NodeRuntimeOverride(adapter="pi_q"),
        "worker_b": NodeRuntimeOverride(adapter="pi_fail"),
    }

    # 1. Mixed: worker_a interrupted (question) + worker_b failed -> thread status = interrupted
    tid_mixed = "par-mixed-state"
    async with WorkflowRunner(
        AdapterRegistry({"pi_q": adapter_q, "pi_fail": adapter_fail}),
        create_default_workflow_registry(),
        persistence=SqlitePersistenceBackend(db_path),
    ) as runner:
        events1 = [
            ev
            async for ev in runner.run(
                _build_parallel_join_workflow,
                "Mixed parallel",
                tmp_path,
                thread_id=tid_mixed,
                node_overrides=mixed_overrides,
                interaction_mode="suspend",
            )
        ]
        st_mixed = await runner.get_thread_state(_build_parallel_join_workflow, tid_mixed)
        assert isinstance(events1[-1].payload, WorkflowInterruptedEvent)
        assert st_mixed.status == "interrupted"
        kinds = {i.node_id: i.kind for i in st_mixed.interrupts}
        assert kinds == {"worker_a": "agent_interaction", "worker_b": "failed"}

        # Must block run() while worker_a has an unanswered interaction!
        with pytest.raises(ValueError, match="interrupted"):
            await anext(
                runner.run(
                    _build_parallel_join_workflow,
                    "Should block",
                    tmp_path,
                    thread_id=tid_mixed,
                    node_overrides=mixed_overrides,
                )
            )

        req_a = next(i.interaction for i in st_mixed.interrupts if i.node_id == "worker_a")
        assert req_a is not None
        resumed = [
            ev
            async for ev in runner.resume(
                _build_parallel_join_workflow,
                tid_mixed,
                node_overrides=mixed_overrides,
                resumes={
                    "worker_a": ResumeInput(
                        interaction_response=InteractionResponse(
                            request_id=req_a.request_id, decision="approve", value="redis"
                        )
                    ),
                    "worker_b": ResumeInput(prompt="Retry worker_b"),
                },
            )
        ]

    _write_manifest(tmp_path, "parallel_mixed_resume", resumed)
    assert isinstance(resumed[-1].payload, WorkflowCompletedEvent)
    assert resumed[-1].payload.status == "completed"
    assert "redis" in resumed[-1].payload.final_output
    assert "EXECUTOR_RECOVERED(turns=2):Retry worker_b" in resumed[-1].payload.final_output

    # 2. Dual failure -> run() supersedes BOTH failed branches and preserves BOTH session_refs
    tid_dual_fail = "par-dual-fail"
    async with WorkflowRunner(
        AdapterRegistry({"pi": adapter_fail}),
        create_default_workflow_registry(),
        persistence=SqlitePersistenceBackend(db_path),
    ) as runner_fail:
        fail_events = [
            ev
            async for ev in runner_fail.run(
                _build_parallel_join_workflow, "Fail both", tmp_path, thread_id=tid_dual_fail
            )
        ]
        st_fail = await runner_fail.get_thread_state(_build_parallel_join_workflow, tid_dual_fail)

    assert isinstance(fail_events[-1].payload, WorkflowCompletedEvent)
    assert fail_events[-1].payload.status == "failed"
    assert st_fail.status == "failed"
    assert {i.node_id for i in st_fail.interrupts} == {"worker_a", "worker_b"}
    fail_ref_a = st_fail.node_outputs["worker_a"].session_ref
    fail_ref_b = st_fail.node_outputs["worker_b"].session_ref
    assert fail_ref_a is not None and fail_ref_b is not None

    async with WorkflowRunner(
        AdapterRegistry({"pi": adapter_ok}),
        create_default_workflow_registry(),
        persistence=SqlitePersistenceBackend(db_path),
    ) as runner_ok:
        ok_events = [
            ev
            async for ev in runner_ok.run(
                _build_parallel_join_workflow, "Fresh run", tmp_path, thread_id=tid_dual_fail
            )
        ]
        st_ok = await runner_ok.get_thread_state(_build_parallel_join_workflow, tid_dual_fail)

    assert isinstance(ok_events[-1].payload, WorkflowCompletedEvent)
    assert ok_events[-1].payload.status == "completed"
    assert st_ok.node_outputs["worker_a"].session_ref == fail_ref_a
    assert st_ok.node_outputs["worker_b"].session_ref == fail_ref_b


async def test_parallel_hard_kill_crash_recovers_progressively_preserving_both_sessions(
    tmp_path: Path,
) -> None:
    """Gap: parallel WAL crashes must materialize both branches and allow progressive resume."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "parallel-hard-kill"

    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        str(tmp_path),
        thread_id,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert worker.stdout is not None
        line = (await asyncio.wait_for(worker.stdout.readline(), 15)).decode().strip()
        req_by_node = json.loads(line)
        assert set(req_by_node) == {"worker_a", "worker_b"}
    finally:
        await _hard_kill_worker(worker)

    async with _make_runner("ui_question_unique", checkpoint_db=db_path) as runner:
        st_crash = await runner.get_thread_state(_build_parallel_join_workflow, thread_id)
        assert st_crash.status == "crashed"
        assert {i.node_id for i in st_crash.interrupts} == {"worker_a", "worker_b"}
        assert {r.node_id for r in st_crash.inflight_nodes} == {"worker_a", "worker_b"}
        ref_a = st_crash.node_outputs["worker_a"].session_ref
        ref_b = st_crash.node_outputs["worker_b"].session_ref
        assert ref_a is not None and ref_b is not None

        # Answer worker_a first: Loop 1 materializes both WAL rows; Loop 2 resumes only worker_a.
        ev_a = [
            ev
            async for ev in runner.resume(
                _build_parallel_join_workflow,
                thread_id,
                interaction_response=InteractionResponse(
                    request_id=req_by_node["worker_a"], decision="approve", value="redis"
                ),
            )
        ]
        assert isinstance(ev_a[0].payload, WorkflowResumedEvent)
        assert ev_a[0].payload.resumed_from == "crash"
        assert [ev.payload.node_id for ev in ev_a if ev.payload.type == "node.started"] == [
            "worker_a"
        ]
        assert isinstance(ev_a[-1].payload, WorkflowInterruptedEvent)
        assert [i.node_id for i in ev_a[-1].payload.interrupts] == ["worker_b"]

        # Now answer worker_b -> aggregator runs and finishes cleanly with both sessions intact.
        ev_b = [
            ev
            async for ev in runner.resume(
                _build_parallel_join_workflow,
                thread_id,
                interaction_response=InteractionResponse(
                    request_id=req_by_node["worker_b"], decision="approve", value="memory"
                ),
            )
        ]
        st_final = await runner.get_thread_state(_build_parallel_join_workflow, thread_id)

    _write_manifest(tmp_path, "parallel_hard_kill_resume_b", ev_b)
    assert isinstance(ev_b[-1].payload, WorkflowCompletedEvent)
    assert ev_b[-1].payload.status == "completed"
    assert "redis" in ev_b[-1].payload.final_output and "memory" in ev_b[-1].payload.final_output
    assert st_final.status == "completed" and st_final.inflight_nodes == ()
    assert st_final.node_outputs["worker_a"].session_ref == ref_a
    assert st_final.node_outputs["worker_b"].session_ref == ref_b


async def test_inflight_table_schema_mismatch_fails_fast_on_startup(tmp_path: Path) -> None:
    """Gap: stale care_inflight_nodes schema previously crashed lazily on SELECT."""
    db_path = tmp_path / "stale_schema.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE care_inflight_nodes (
                thread_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                task_id TEXT,
                session_ref TEXT,
                pending_interaction_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (thread_id, node_id)
            )
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="missing=\\['role'\\]"):
        async with _make_runner("standard", checkpoint_db=db_path):
            pass


async def test_workflow_runner_operates_with_custom_non_sqlite_persistence_backend(
    tmp_path: Path,
) -> None:
    """Gap: WorkflowRunner was previously hardcoded to AsyncSqliteSaver and SQLite WAL."""

    class _MemoryInflightStore:
        def __init__(self) -> None:
            self._rows: dict[tuple[str, str], InflightNodeRecord] = {}

        async def upsert(
            self,
            thread_id: str,
            node_id: str,
            *,
            role: str,
            task_id: str,
            session_ref: str | None,
            pending_interaction: InteractionRequestEvent | None,
        ) -> None:
            self._rows[(thread_id, node_id)] = InflightNodeRecord(
                thread_id=thread_id,
                node_id=node_id,
                role=role,
                task_id=task_id,
                session_ref=session_ref,
                pending_interaction=pending_interaction,
                updated_at=datetime.now(UTC).isoformat(),
            )

        async def delete(self, record: InflightNodeRecord) -> None:
            key = (record.thread_id, record.node_id)
            if key in self._rows and self._rows[key].task_id == record.task_id:
                del self._rows[key]

        async def list_for_thread(self, thread_id: str) -> list[InflightNodeRecord]:
            return [r for (tid, _), r in self._rows.items() if tid == thread_id]

    class _MemoryPersistenceBackend:
        def __init__(self) -> None:
            self.checkpointer = InMemorySaver()
            self.inflight_store = _MemoryInflightStore()

        async def __aenter__(self) -> _MemoryPersistenceBackend:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

    adapter = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "standard"),
            request_timeout_s=10.0,
        )
    )
    backend = _MemoryPersistenceBackend()
    async with WorkflowRunner(
        AdapterRegistry({"pi": adapter}),
        create_default_workflow_registry(),
        persistence=backend,
    ) as runner:
        run1 = [
            ev
            async for ev in runner.run(
                "plan_and_execute", "Memory backend", tmp_path, thread_id="m1"
            )
        ]
        assert isinstance(run1[-1].payload, WorkflowInterruptedEvent)
        run2 = [
            ev
            async for ev in runner.resume(
                "plan_and_execute", "m1", resume_value={"decision": "approve"}
            )
        ]
        st = await runner.get_thread_state("plan_and_execute", "m1")

    assert isinstance(run2[-1].payload, WorkflowCompletedEvent)
    assert run2[-1].payload.status == "completed"
    assert st.status == "completed"
    assert st.inflight_nodes == ()


async def test_multi_turn_resets_stale_node_outputs_while_preserving_node_sessions(
    tmp_path: Path,
) -> None:
    """Gap: reusing node_outputs for session_ref leaked stale outputs on skipped branches."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "branch-reset-thread"

    def _build_branching_workflow() -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "branch_a",
            build_agent_node(
                AgentNodeConfig(
                    id="branch_a",
                    role="branch_a",
                    prompt_builder=lambda s: s["prompt"],
                )
            ),
        )
        graph.add_node(
            "branch_b",
            build_agent_node(
                AgentNodeConfig(
                    id="branch_b",
                    role="branch_b",
                    prompt_builder=lambda s: s["prompt"],
                )
            ),
        )
        graph.add_conditional_edges(
            START,
            lambda s: "branch_a" if s["prompt"].startswith("A:") else "branch_b",
            {"branch_a": "branch_a", "branch_b": "branch_b"},
        )
        graph.add_edge("branch_a", END)
        graph.add_edge("branch_b", END)
        return graph

    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner:
        async for _ in runner.run(
            _build_branching_workflow, "A: first", tmp_path, thread_id=thread_id
        ):
            pass
        st1 = await runner.get_thread_state(_build_branching_workflow, thread_id)
        ref_a = st1.node_sessions["branch_a"]
        assert set(st1.node_outputs) == {"branch_a"}

        # Turn 2 takes branch_b: branch_a's stale NodeOutput is cleared, its session_ref stays!
        async for _ in runner.run(
            _build_branching_workflow, "B: second", tmp_path, thread_id=thread_id
        ):
            pass
        st2 = await runner.get_thread_state(_build_branching_workflow, thread_id)
        assert set(st2.node_outputs) == {"branch_b"}
        assert "branch_a" not in st2.node_outputs
        assert st2.node_sessions["branch_a"] == ref_a
        assert "branch_b" in st2.node_sessions

        # Turn 3 returns to branch_a and resumes the original Turn-1 session_ref.
        ev3 = [
            ev
            async for ev in runner.run(
                _build_branching_workflow, "A: third", tmp_path, thread_id=thread_id
            )
        ]

    final3 = ev3[-1].payload
    assert isinstance(final3, WorkflowCompletedEvent)
    payload3 = json.loads(final3.final_output)
    assert payload3["session_file"] == ref_a
    assert payload3["history"] == ["A: first", "A: third"]


async def test_heterogeneous_per_node_overrides_route_distinct_endpoints_and_adapters(
    tmp_path: Path,
) -> None:
    """Gap: WorkflowBuilder previously baked one global adapter/endpoint/thinking across nodes."""

    def _build_isolated_plan_exec() -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "planner",
            build_agent_node(
                AgentNodeConfig(
                    id="planner",
                    role="planner",
                    tool_policy="read_only",
                    prompt_builder=lambda s: f"Plan: {s['prompt']}",
                )
            ),
        )
        graph.add_node(
            "plan_approval",
            build_approval_node("plan_approval", message="Approve?"),
        )
        graph.add_node(
            "executor",
            build_agent_node(
                AgentNodeConfig(
                    id="executor",
                    role="executor",
                    prompt_builder=lambda s: s["node_outputs"]["planner"].summary,
                )
            ),
        )
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "plan_approval")
        graph.add_conditional_edges(
            "plan_approval",
            continue_if_running("executor"),
            {"executor": "executor", END: END},
        )
        graph.add_edge("executor", END)
        return graph

    adapter_a = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "inspect_endpoint"),
            request_timeout_s=10.0,
        )
    )
    adapter_b = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "inspect_endpoint"),
            request_timeout_s=10.0,
        )
    )
    overrides = {
        "planner": NodeRuntimeOverride(
            adapter="planner_backend",
            endpoint=ModelEndpoint(
                model="claude-sonnet-4-5",
                base_url="https://planner.example.internal",
                api_protocol="anthropic-messages",
            ),
            thinking_level="high",
        ),
        "executor": NodeRuntimeOverride(
            adapter="executor_backend",
            endpoint=ModelEndpoint(
                model="gpt-5-codex",
                base_url="https://executor.example.internal",
                api_protocol="openai-responses",
            ),
            thinking_level="low",
        ),
    }

    async with WorkflowRunner(
        AdapterRegistry({"planner_backend": adapter_a, "executor_backend": adapter_b}),
        create_default_workflow_registry(),
    ) as runner:
        ev1 = [
            ev
            async for ev in runner.run(
                _build_isolated_plan_exec,
                "Heterogeneous task",
                tmp_path,
                thread_id="het-1",
                node_overrides=overrides,
            )
        ]
        assert isinstance(ev1[-1].payload, WorkflowInterruptedEvent)
        ev2 = [
            ev
            async for ev in runner.resume(
                _build_isolated_plan_exec,
                "het-1",
                resume_value={"decision": "approve"},
                node_overrides=overrides,
            )
        ]
        st = await runner.get_thread_state(_build_isolated_plan_exec, "het-1")

    planner_started = next(
        ev.payload
        for ev in ev1
        if ev.payload.type == "node.started" and ev.payload.node_id == "planner"
    )
    executor_started = next(
        ev.payload
        for ev in ev2
        if ev.payload.type == "node.started" and ev.payload.node_id == "executor"
    )
    assert planner_started.adapter == "planner_backend"
    assert executor_started.adapter == "executor_backend"

    planner_report = json.loads(st.node_outputs["planner"].summary)
    executor_report = json.loads(st.node_outputs["executor"].summary)
    assert planner_report["model"] == "claude-sonnet-4-5"
    assert planner_report["thinking"] == "high"
    assert executor_report["model"] == "gpt-5-codex"
    assert executor_report["thinking"] == "low"


async def test_parallel_approval_gates_preserve_aborted_status_deterministically(
    tmp_path: Path,
) -> None:
    """Gap: _last_value reducer on WorkflowState.status could drop 'aborted' in parallel gates."""

    def _build_parallel_gates() -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node("gate_a", build_approval_node("gate_a", message="Approve A?"))
        graph.add_node("gate_b", build_approval_node("gate_b", message="Approve B?"))
        graph.add_node(
            "after",
            build_agent_node(
                AgentNodeConfig(
                    id="after",
                    role="executor",
                    prompt_builder=lambda s: s["prompt"],
                )
            ),
        )
        graph.add_edge(START, "gate_a")
        graph.add_edge(START, "gate_b")
        graph.add_conditional_edges(
            "gate_a", continue_if_running("after"), {"after": "after", END: END}
        )
        graph.add_conditional_edges(
            "gate_b", continue_if_running("after"), {"after": "after", END: END}
        )
        graph.add_edge("after", END)
        return graph

    async with _make_runner("standard", checkpoint_db=":memory:") as runner:
        ev1 = [
            ev
            async for ev in runner.run(
                _build_parallel_gates, "Check gates", tmp_path, thread_id="par-gates"
            )
        ]
        assert isinstance(ev1[-1].payload, WorkflowInterruptedEvent)
        assert {i.node_id for i in ev1[-1].payload.interrupts} == {"gate_a", "gate_b"}

        ev2 = [
            ev
            async for ev in runner.resume(
                _build_parallel_gates,
                "par-gates",
                resumes={
                    "gate_a": ResumeInput(resume_value={"decision": "deny", "feedback": "no"}),
                    "gate_b": ResumeInput(resume_value={"decision": "approve"}),
                },
            )
        ]

    final = ev2[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "aborted"
    assert not any(
        ev.payload.type == "node.started" and ev.payload.node_id == "after" for ev in ev2
    )


async def test_adapter_capabilities_are_enforced_fail_fast(tmp_path: Path) -> None:
    """Gap: AgentCapabilities was declared but never checked by the orchestrator."""

    class _LimitedPiAdapter(PiRpcAdapter):
        kind = "limited"
        capabilities = AgentCapabilities(
            streaming=True,
            steering=False,
            follow_up=False,
            abort=False,
            session_resume=False,
            interactions=False,
        )

    adapter = _LimitedPiAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "steerable"),
            request_timeout_s=10.0,
        )
    )
    async with WorkflowRunner(
        AdapterRegistry({"limited": adapter}),
        create_default_workflow_registry(),
    ) as runner:
        async def _drive() -> None:
            async for ev in runner.run(
                "single_agent",
                "Cap check",
                tmp_path,
                run_id="cap-run-1",
                thread_id="cap-thread-1",
                adapter="limited",
            ):
                if isinstance(ev.payload, TextDeltaEvent):
                    with pytest.raises(AdapterCapabilityError, match="steering"):
                        await runner.steer("cap-run-1", "cannot steer")
                    with pytest.raises(AdapterCapabilityError, match="follow-up"):
                        await runner.follow_up("cap-run-1", "cannot follow up")
                    with pytest.raises(AdapterCapabilityError, match="abort"):
                        await runner.abort("cap-run-1")
                    # Complete turn directly via the session's underlying subprocess by steering
                    # after restoring capability? No — cancel consumer cleanly to end test.
                    break

        await _drive()

    # Verify session_resume=False fails fast on Turn 2 when persisted session_ref exists
    resume_adapter = _LimitedPiAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "stateful_chat"),
            request_timeout_s=10.0,
        )
    )
    async with WorkflowRunner(
        AdapterRegistry({"limited": resume_adapter}),
        create_default_workflow_registry(),
    ) as runner2:
        async for _ in runner2.run(
            "single_agent",
            "Turn 1",
            tmp_path,
            thread_id="cap-resume-thread",
            adapter="limited",
        ):
            pass
        with pytest.raises(AdapterCapabilityError, match="resuming persisted sessions"):
            async for _ in runner2.run(
                "single_agent",
                "Turn 2",
                tmp_path,
                thread_id="cap-resume-thread",
                adapter="limited",
            ):
                pass


def _build_interactive_handoff_workflow() -> StateGraph[WorkflowState]:
    """2-node workflow: interactive planner (exits on submit_plan tool) -> one-shot executor."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                tool_policy="read_only",
                append_system_prompt="You are the Planner agent.",
                prompt_builder=lambda s: s["prompt"],
                should_complete=lambda _output, tools: any(
                    t.tool_name == "submit_plan" and not t.is_error for t in tools
                ),
            )
        ),
    )
    graph.add_node(
        "executor",
        build_agent_node(
            AgentNodeConfig(
                id="executor",
                role="executor",
                prompt_builder=lambda s: (
                    f"ORIGINAL={s['prompt']}\nPLAN={s['node_outputs']['planner'].summary}"
                ),
            )
        ),
    )
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", END)
    return graph


def _build_explicit_handoff_workflow() -> StateGraph[WorkflowState]:
    """2-node workflow: planner stays interactive until explicit completion -> executor."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                tool_policy="read_only",
                append_system_prompt="You are the Planner agent.",
                prompt_builder=lambda s: s["prompt"],
                should_complete=lambda _output, _tools: False,
            )
        ),
    )
    graph.add_node(
        "executor",
        build_agent_node(
            AgentNodeConfig(
                id="executor",
                role="executor",
                prompt_builder=lambda s: f"HANDOFF={s['node_outputs']['planner'].summary}",
            )
        ),
    )
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "executor")
    graph.add_edge("executor", END)
    return graph


def _build_parallel_interactive_workflow() -> StateGraph[WorkflowState]:
    """Parallel interactive branches joining into a one-shot aggregator."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "worker_a",
        build_agent_node(
            AgentNodeConfig(
                id="worker_a",
                role="worker",
                prompt_builder=lambda s: f"A:{s['prompt']}",
                should_complete=lambda _output, _tools: False,
            )
        ),
    )
    graph.add_node(
        "worker_b",
        build_agent_node(
            AgentNodeConfig(
                id="worker_b",
                role="worker",
                prompt_builder=lambda s: f"B:{s['prompt']}",
                should_complete=lambda _output, _tools: False,
            )
        ),
    )
    graph.add_node(
        "aggregator",
        build_agent_node(
            AgentNodeConfig(
                id="aggregator",
                role="aggregator",
                prompt_builder=lambda s: (
                    f"JOIN|A={s['node_outputs']['worker_a'].summary}"
                    f"|B={s['node_outputs']['worker_b'].summary}"
                ),
            )
        ),
    )
    graph.add_edge(START, "worker_a")
    graph.add_edge(START, "worker_b")
    graph.add_edge("worker_a", "aggregator")
    graph.add_edge("worker_b", "aggregator")
    graph.add_edge("aggregator", END)
    return graph


async def test_interactive_node_chats_across_restarts_and_advances_on_handoff_tool(
    tmp_path: Path,
) -> None:
    """Interactive planner stays in same session across turns until submit_plan succeeds."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "interactive-tool-handoff"

    # Turn 1: planner responds without calling submit_plan -> pauses in awaiting_input
    t1_events: list[RuntimeEvent] = []
    async with _make_runner("interactive_handoff", checkpoint_db=db_path) as runner1:
        async for ev in runner1.run(
            _build_interactive_handoff_workflow,
            "Draft caching plan",
            tmp_path,
            thread_id=thread_id,
        ):
            t1_events.append(ev)
        st1 = await runner1.get_thread_state(_build_interactive_handoff_workflow, thread_id)

    _write_manifest(tmp_path, "interactive_turn1", t1_events)
    assert isinstance(t1_events[-1].payload, WorkflowInterruptedEvent)
    assert len(t1_events[-1].payload.interrupts) == 1
    intr1 = t1_events[-1].payload.interrupts[0]
    assert intr1.node_id == "planner"
    assert intr1.kind == "awaiting_input"
    assert intr1.payload["summary"]
    assert not any(ev.payload.type == "node.completed" for ev in t1_events)
    assert st1.status == "interrupted" and st1.resumable is True
    assert st1.node_outputs["planner"].status == "waiting"
    planner_ref = st1.node_outputs["planner"].session_ref
    assert planner_ref is not None

    # Turn 2..4 in brand-new Runner simulating restart: validate fail-fast, chat, failed tool,
    # then successful submit_plan advances automatically to executor.
    async with _make_runner("interactive_handoff", checkpoint_db=db_path) as runner2:
        with pytest.raises(ValueError, match="is interrupted"):
            await anext(
                runner2.run(
                    _build_interactive_handoff_workflow,
                    "Overlap",
                    tmp_path,
                    thread_id=thread_id,
                )
            )
        with pytest.raises(ValueError, match="requires a non-empty 'prompt'"):
            await anext(runner2.resume(_build_interactive_handoff_workflow, thread_id))
        with pytest.raises(ValueError, match="does not accept resume_value"):
            await anext(
                runner2.resume(
                    _build_interactive_handoff_workflow,
                    thread_id,
                    resume_value={"action": "complete"},
                )
            )
        with pytest.raises(ValueError, match="cannot combine"):
            await anext(
                runner2.resume(
                    _build_interactive_handoff_workflow,
                    thread_id,
                    prompt="both",
                    complete=True,
                )
            )
        with pytest.raises(ValueError, match="does not accept interaction_response"):
            await anext(
                runner2.resume(
                    _build_interactive_handoff_workflow,
                    thread_id,
                    interaction_response=InteractionResponse(request_id="x", decision="approve"),
                )
            )

        t2_events = [
            ev
            async for ev in runner2.resume(
                _build_interactive_handoff_workflow,
                thread_id,
                prompt="Switch to Redis",
            )
        ]
        assert isinstance(t2_events[0].payload, WorkflowResumedEvent)
        assert t2_events[0].payload.resumed_from == "awaiting_input"
        assert isinstance(t2_events[-1].payload, WorkflowInterruptedEvent)
        assert t2_events[-1].payload.interrupts[0].kind == "awaiting_input"
        t2_summary = json.loads(t2_events[-1].payload.interrupts[0].payload["summary"])
        assert t2_summary["session_file"] == planner_ref
        assert t2_summary["turn_count"] == 2
        assert t2_summary["history"] == ["Draft caching plan", "Switch to Redis"]
        assert not any(
            ev.payload.type == "node.started" and ev.payload.node_id == "executor"
            for ev in t2_events
        )

        # Failed submit_plan tool call must NOT complete the planner node.
        t3_events = [
            ev
            async for ev in runner2.resume(
                _build_interactive_handoff_workflow,
                thread_id,
                prompt="FAIL_SUBMIT",
            )
        ]
        assert isinstance(t3_events[-1].payload, WorkflowInterruptedEvent)
        assert t3_events[-1].payload.interrupts[0].kind == "awaiting_input"

        # Successful submit_plan tool call completes planner and hands off to executor.
        t4_events = [
            ev
            async for ev in runner2.resume(
                _build_interactive_handoff_workflow,
                thread_id,
                prompt="Approved, SUBMIT_PLAN",
            )
        ]
        final_state = await runner2.get_thread_state(
            _build_interactive_handoff_workflow, thread_id
        )

    _write_manifest(tmp_path, "interactive_turn4_handoff", t4_events)
    started = [ev.payload.node_id for ev in t4_events if ev.payload.type == "node.started"]
    completed = [
        (ev.payload.node_id, ev.payload.status)
        for ev in t4_events
        if ev.payload.type == "node.completed"
    ]
    assert started == ["planner", "executor"]
    assert completed == [("planner", "completed"), ("executor", "completed")]
    assert isinstance(t4_events[-1].payload, WorkflowCompletedEvent)
    assert t4_events[-1].payload.status == "completed"

    exec_payload = json.loads(t4_events[-1].payload.final_output)
    assert exec_payload["role"] == "executor"
    assert exec_payload["session_file"] != planner_ref
    assert "ORIGINAL=Draft caching plan" in exec_payload["history"][0]
    assert "Approved, SUBMIT_PLAN" in exec_payload["history"][0]
    assert final_state.status == "completed" and not final_state.resumable
    assert final_state.node_outputs["planner"].status == "completed"
    assert final_state.node_outputs["planner"].session_ref == planner_ref


async def test_interactive_node_explicit_completion_skips_extra_turn_and_supports_discard(
    tmp_path: Path,
) -> None:
    """Explicit action='complete' exits await_input directly without an extra LLM turn."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "explicit-handoff"

    async with _make_runner("interactive_handoff", checkpoint_db=db_path) as runner:
        e1 = [
            ev
            async for ev in runner.run(
                _build_explicit_handoff_workflow,
                "Initial brief",
                tmp_path,
                thread_id=thread_id,
            )
        ]
        assert isinstance(e1[-1].payload, WorkflowInterruptedEvent)

        e2 = [
            ev
            async for ev in runner.resume(
                _build_explicit_handoff_workflow,
                thread_id,
                prompt="Add rollback section",
            )
        ]
        assert isinstance(e2[-1].payload, WorkflowInterruptedEvent)

        # User explicitly completes planner: no new planner turn should start!
        e3 = [
            ev
            async for ev in runner.resume(
                _build_explicit_handoff_workflow,
                thread_id,
                complete=True,
            )
        ]
        st_done = await runner.get_thread_state(_build_explicit_handoff_workflow, thread_id)

        _write_manifest(tmp_path, "explicit_complete", e3)
        started = [ev.payload.node_id for ev in e3 if ev.payload.type == "node.started"]
        completed = [
            (ev.payload.node_id, ev.payload.status)
            for ev in e3
            if ev.payload.type == "node.completed"
        ]
        assert started == ["executor"]
        assert completed == [("planner", "completed"), ("executor", "completed")]
        assert isinstance(e3[-1].payload, WorkflowCompletedEvent)
        exec_payload = json.loads(e3[-1].payload.final_output)
        assert "Add rollback section" in exec_payload["history"][0]
        assert st_done.status == "completed" and not st_done.resumable

        # complete=True on a non-awaiting_input interrupt (plan_approval) must fail fast.
        async for _ in runner.run("plan_and_execute", "Check gate", tmp_path, thread_id="gate-chk"):
            pass
        with pytest.raises(ValueError, match="only valid for awaiting_input"):
            await anext(runner.resume("plan_and_execute", "gate-chk", complete=True))

        # Discard while awaiting_input preserves planner session_ref for a fresh run.
        thread_discard = "explicit-handoff-discard"
        async for _ in runner.run(
            _build_explicit_handoff_workflow,
            "Draft before discard",
            tmp_path,
            thread_id=thread_discard,
        ):
            pass
        st_wait = await runner.get_thread_state(_build_explicit_handoff_workflow, thread_discard)
        saved_ref = st_wait.node_outputs["planner"].session_ref
        assert saved_ref is not None

        discarded = await runner.discard(_build_explicit_handoff_workflow, thread_discard)
        assert discarded.status == "aborted" and not discarded.resumable
        assert discarded.node_sessions["planner"] == saved_ref

        fresh_events = [
            ev
            async for ev in runner.run(
                _build_explicit_handoff_workflow,
                "Fresh draft after discard",
                tmp_path,
                thread_id=thread_discard,
            )
        ]
        fresh_summary = json.loads(fresh_events[-1].payload.interrupts[0].payload["summary"])
        assert fresh_summary["session_file"] == saved_ref
        assert fresh_summary["history"] == ["Draft before discard", "Fresh draft after discard"]


async def test_parallel_interactive_branches_chat_and_complete_independently(
    tmp_path: Path,
) -> None:
    """Parallel awaiting_input branches can chat or complete independently before joining."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "parallel-interactive"

    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner:
        e1 = [
            ev
            async for ev in runner.run(
                _build_parallel_interactive_workflow,
                "Design system",
                tmp_path,
                thread_id=thread_id,
            )
        ]
        assert isinstance(e1[-1].payload, WorkflowInterruptedEvent)
        assert {item.node_id: item.kind for item in e1[-1].payload.interrupts} == {
            "worker_a": "awaiting_input",
            "worker_b": "awaiting_input",
        }

        # Chat more on worker_a while completing worker_b in a single batch resume.
        e2 = [
            ev
            async for ev in runner.resume(
                _build_parallel_interactive_workflow,
                thread_id,
                resumes={
                    "worker_a": ResumeInput(prompt="Expand section A"),
                    "worker_b": ResumeInput(complete=True),
                },
            )
        ]
        assert isinstance(e2[-1].payload, WorkflowInterruptedEvent)
        assert [(i.node_id, i.kind) for i in e2[-1].payload.interrupts] == [
            ("worker_a", "awaiting_input")
        ]
        assert not any(
            ev.payload.type == "node.started" and ev.payload.node_id == "aggregator" for ev in e2
        )

        # Complete worker_a -> aggregator runs with both final summaries.
        e3 = [
            ev
            async for ev in runner.resume(
                _build_parallel_interactive_workflow,
                thread_id,
                complete=True,
            )
        ]

    _write_manifest(tmp_path, "parallel_interactive_join", e3)
    assert isinstance(e3[-1].payload, WorkflowCompletedEvent)
    assert e3[-1].payload.status == "completed"
    agg_payload = json.loads(e3[-1].payload.final_output)
    assert "Expand section A" in agg_payload["history"][0]
    assert "B:Design system" in agg_payload["history"][0]


async def test_plan_and_execute_inherits_planner_context_via_fork_and_reforks_on_new_turn(
    tmp_path: Path,
) -> None:
    """Executor forks planner's session without mutating parent, and re-forks on a new turn."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "plan-inherit-fork"

    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner:
        # Turn 1: planner -> approval(with note) -> executor (forks planner session)
        t1_plan = [
            ev
            async for ev in runner.run(
                "plan_and_execute", "Feature alpha", tmp_path, thread_id=thread_id
            )
        ]
        assert isinstance(t1_plan[-1].payload, WorkflowInterruptedEvent)
        async for _ in runner.resume(
            "plan_and_execute",
            thread_id,
            resume_value={"decision": "approve", "feedback": "Add unit tests"},
        ):
            pass
        st1 = await runner.get_thread_state("plan_and_execute", thread_id)

        plan_ref_1 = st1.node_outputs["planner"].session_ref
        exec_ref_1 = st1.node_outputs["executor"].session_ref
        assert plan_ref_1 is not None and exec_ref_1 is not None
        assert plan_ref_1 != exec_ref_1

        exec_payload_1 = json.loads(st1.node_outputs["executor"].summary)
        assert exec_payload_1["session_file"] == exec_ref_1
        assert exec_payload_1["turn_count"] == 2
        assert "Currently in the **Plan** phase" in exec_payload_1["history"][0]
        assert "Feature alpha" in exec_payload_1["history"][0]
        assert "Currently in the **Execute** phase" in exec_payload_1["history"][1]
        assert "## Approval Note\nAdd unit tests" in exec_payload_1["history"][1]

        # Parent planner session file must remain unpolluted by executor's turn.
        parent_lines_t1 = Path(plan_ref_1).read_text(encoding="utf-8").splitlines()
        assert len(parent_lines_t1) == 1

        # Turn 2 on same thread: planner continues its clean session; executor forks fresh from it.
        t2_plan = [
            ev
            async for ev in runner.run(
                "plan_and_execute", "Feature beta", tmp_path, thread_id=thread_id
            )
        ]
        assert isinstance(t2_plan[-1].payload, WorkflowInterruptedEvent)
        t2_exec = [
            ev
            async for ev in runner.resume(
                "plan_and_execute",
                thread_id,
                resume_value={"decision": "approve", "feedback": "Keep API backwards-compatible"},
            )
        ]
        st2 = await runner.get_thread_state("plan_and_execute", thread_id)

    _write_manifest(tmp_path, "plan_inherit_fork_turn2", [*t2_plan, *t2_exec])
    assert st2.node_outputs["planner"].session_ref == plan_ref_1
    exec_ref_2 = st2.node_outputs["executor"].session_ref
    assert exec_ref_2 is not None and exec_ref_2 not in (plan_ref_1, exec_ref_1)

    plan_payload_2 = json.loads(st2.node_outputs["planner"].summary)
    assert plan_payload_2["turn_count"] == 2
    assert "Feature alpha" in plan_payload_2["history"][0]
    assert "Feature beta" in plan_payload_2["history"][1]

    exec_payload_2 = json.loads(st2.node_outputs["executor"].summary)
    assert exec_payload_2["session_file"] == exec_ref_2
    assert exec_payload_2["turn_count"] == 3
    assert "Feature alpha" in exec_payload_2["history"][0]
    assert "Feature beta" in exec_payload_2["history"][1]
    assert "## Approval Note\nKeep API backwards-compatible" in exec_payload_2["history"][2]


async def test_interactive_sop_node_then_approval_then_inheriting_node(tmp_path: Path) -> None:
    """Interactive multi-turn SOP node -> approval -> downstream node inheriting full history."""
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "sop-prd-inherit"
    shared_prompt = "Follow standard enterprise engineering SOP."

    def _build_sop_workflow() -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "draft_prd",
            build_agent_node(
                AgentNodeConfig(
                    id="draft_prd",
                    role="prd",
                    append_system_prompt=shared_prompt,
                    tools=("read", "write", "grep"),
                    prompt_builder=lambda s: f"Draft PRD:\n{s['prompt']}",
                    should_complete=lambda _o, _t: False,
                )
            ),
        )
        graph.add_node(
            "prd_approval",
            build_approval_node("prd_approval", message="确认定稿并进入开发？"),
        )
        graph.add_node(
            "implement",
            build_agent_node(
                AgentNodeConfig(
                    id="implement",
                    role="developer",
                    inherit_session_from="draft_prd",
                    prompt_builder=lambda s: (
                        "PRD approved "
                        f"({s['node_outputs']['prd_approval'].summary}). "
                        "Start implementation."
                    ),
                )
            ),
        )
        graph.add_edge(START, "draft_prd")
        graph.add_edge("draft_prd", "prd_approval")
        graph.add_conditional_edges(
            "prd_approval",
            continue_if_running("implement"),
            {"implement": "implement", END: END},
        )
        graph.add_edge("implement", END)
        return graph

    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner:
        e1 = [
            ev
            async for ev in runner.run(
                _build_sop_workflow, "Build order export", tmp_path, thread_id=thread_id
            )
        ]
        assert isinstance(e1[-1].payload, WorkflowInterruptedEvent)
        assert e1[-1].payload.interrupts[0].kind == "awaiting_input"

        # Iterate freely inside draft_prd
        e2 = [
            ev
            async for ev in runner.resume(
                _build_sop_workflow, thread_id, prompt="Add CSV and Excel support"
            )
        ]
        assert isinstance(e2[-1].payload, WorkflowInterruptedEvent)
        assert e2[-1].payload.interrupts[0].kind == "awaiting_input"

        # Finalize draft_prd -> enters prd_approval
        e3 = [ev async for ev in runner.resume(_build_sop_workflow, thread_id, complete=True)]
        assert isinstance(e3[-1].payload, WorkflowInterruptedEvent)
        assert e3[-1].payload.interrupts[0].node_id == "prd_approval"

        # Approve -> implement forks full 2-turn draft_prd context + approval feedback
        e4 = [
            ev
            async for ev in runner.resume(
                _build_sop_workflow,
                thread_id,
                resume_value={"decision": "approve", "feedback": "Ship CSV first"},
            )
        ]
        st = await runner.get_thread_state(_build_sop_workflow, thread_id)

    _write_manifest(tmp_path, "sop_prd_inherit", e4)
    assert isinstance(e4[-1].payload, WorkflowCompletedEvent)
    assert e4[-1].payload.status == "completed"
    impl_payload = json.loads(st.node_outputs["implement"].summary)
    assert impl_payload["turn_count"] == 3
    assert impl_payload["session_file"] != st.node_outputs["draft_prd"].session_ref
    assert "Build order export" in impl_payload["history"][0]
    assert impl_payload["history"][1] == "Add CSV and Excel support"
    assert "approved: Ship CSV first" in impl_payload["history"][2]


async def test_session_inheritance_auto_inherits_warns_on_override_and_supports_lenient_mode(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Inheriting nodes auto-inherit settings, warn on overrides, and fail fast on hard limits."""
    # Build-time validation: blank, self-reference, or lenient without inherit_session_from.
    with pytest.raises(ValueError, match="inherit_session_from"):
        build_agent_node(
            AgentNodeConfig(
                id="node_a",
                role="a",
                inherit_session_from="   ",
                prompt_builder=lambda s: s["prompt"],
            )
        )
    with pytest.raises(ValueError, match="cannot inherit session from itself"):
        build_agent_node(
            AgentNodeConfig(
                id="node_a",
                role="a",
                inherit_session_from="node_a",
                prompt_builder=lambda s: s["prompt"],
            )
        )
    with pytest.raises(ValueError, match="inherit_session_lenient=True requires"):
        build_agent_node(
            AgentNodeConfig(
                id="node_a",
                role="a",
                inherit_session_lenient=True,
                prompt_builder=lambda s: s["prompt"],
            )
        )

    def _two_node_wf(
        *,
        src_cfg: dict[str, Any] | None = None,
        dst_cfg: dict[str, Any] | None = None,
        skip_source: bool = False,
    ) -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        if not skip_source:
            graph.add_node(
                "src",
                build_agent_node(
                    AgentNodeConfig(
                        id="src",
                        role="src",
                        prompt_builder=lambda s: s["prompt"],
                        **(src_cfg or {}),
                    )
                ),
            )
        graph.add_node(
            "dst",
            build_agent_node(
                AgentNodeConfig(
                    id="dst",
                    role="dst",
                    inherit_session_from="src",
                    prompt_builder=lambda s: f"Next: {s['prompt']}",
                    **(dst_cfg or {}),
                )
            ),
        )
        if skip_source:
            graph.add_edge(START, "dst")
        else:
            graph.add_edge(START, "src")
            graph.add_edge("src", "dst")
        graph.add_edge("dst", END)
        return graph

    # 1. Auto-inheritance: dst omits prompts/tools -> inherits src's settings with zero warnings.
    async with _make_runner("inspect_endpoint", checkpoint_db=":memory:") as inspect_runner:
        with caplog.at_level("WARNING", logger="agent_runtime.orchestrator.node"):
            caplog.clear()
            events_auto = [
                ev
                async for ev in inspect_runner.run(
                    lambda: _two_node_wf(
                        src_cfg={
                            "system_prompt": "Base SOP",
                            "append_system_prompt": "Shared rules",
                            "tools": ("read", "grep"),
                        },
                    ),
                    "Auto inherit",
                    tmp_path,
                    thread_id="auto-inherit-1",
                )
            ]
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
        dst_report = json.loads(events_auto[-1].payload.final_output)
        assert dst_report["system_prompt"] == "Base SOP"
        assert dst_report["append_system_prompt"] == "Shared rules"
        assert dst_report["tools"] == "read,grep"

        # 2. Explicit overrides on dst succeed and emit a warning naming the overridden fields.
        with caplog.at_level("WARNING", logger="agent_runtime.orchestrator.node"):
            caplog.clear()
            events_override = [
                ev
                async for ev in inspect_runner.run(
                    lambda: _two_node_wf(
                        src_cfg={
                            "tool_policy": "read_only",
                            "append_system_prompt": "Plan rules",
                        },
                        dst_cfg={
                            "tool_policy": "full",
                            "append_system_prompt": "Exec rules",
                        },
                    ),
                    "Override with warning",
                    tmp_path,
                    thread_id="override-warn-1",
                )
            ]
        warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warn_records) == 1
        assert "append_system_prompt" in warn_records[0].message
        assert "tool_policy" in warn_records[0].message
        assert "inherit_session_lenient=True" in warn_records[0].message
        override_report = json.loads(events_override[-1].payload.final_output)
        assert override_report["append_system_prompt"] == "Exec rules"
        assert override_report["tools"] is None
        assert override_report["no_tools"] is False

        # 3. inherit_session_lenient=True suppresses the override warning.
        with caplog.at_level("WARNING", logger="agent_runtime.orchestrator.node"):
            caplog.clear()
            events_lenient = [
                ev
                async for ev in inspect_runner.run(
                    lambda: _two_node_wf(
                        src_cfg={"tool_policy": "read_only"},
                        dst_cfg={
                            "tool_policy": "full",
                            "append_system_prompt": "Lenient exec",
                            "inherit_session_lenient": True,
                        },
                    ),
                    "Override lenient",
                    tmp_path,
                    thread_id="override-lenient-1",
                )
            ]
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
        lenient_report = json.loads(events_lenient[-1].payload.final_output)
        assert lenient_report["append_system_prompt"] == "Lenient exec"
        assert lenient_report["tools"] is None

    # 4. Hard Fail-Fast boundaries: persist_session=False and missing source session.
    async with _make_runner("stateful_chat", checkpoint_db=":memory:") as runner:
        with pytest.raises(ValueError, match="requires persist_session=True"):
            async for _ in runner.run(
                lambda: _two_node_wf(),
                "No persist",
                tmp_path,
                persist_session=False,
            ):
                pass

        with pytest.raises(ValueError, match="has not produced a persisted session"):
            async for _ in runner.run(lambda: _two_node_wf(skip_source=True), "No src", tmp_path):
                pass

    # 5. Cross-adapter mismatch on plan_and_execute still fails fast with AdapterCapabilityError.
    adapter_a = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "stateful_chat"),
            request_timeout_s=10.0,
        )
    )
    adapter_b = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "stateful_chat"),
            request_timeout_s=10.0,
        )
    )
    cross_overrides = {
        "planner": NodeRuntimeOverride(adapter="pi_a"),
        "executor": NodeRuntimeOverride(adapter="pi_b"),
    }
    async with WorkflowRunner(
        AdapterRegistry({"pi_a": adapter_a, "pi_b": adapter_b}),
        create_default_workflow_registry(),
    ) as cross_runner:
        async for _ in cross_runner.run(
            "plan_and_execute",
            "Cross adapter",
            tmp_path,
            thread_id="cross-1",
            node_overrides=cross_overrides,
        ):
            pass
        with pytest.raises(AdapterCapabilityError, match="cannot inherit session"):
            async for _ in cross_runner.resume(
                "plan_and_execute",
                "cross-1",
                resume_value={"decision": "approve"},
                node_overrides=cross_overrides,
            ):
                pass

    # 6. Adapter without session_fork capability fails fast with AdapterCapabilityError.
    class _NoForkAdapter:
        kind = "no_fork"
        capabilities = AgentCapabilities(streaming=True, session_resume=True, session_fork=False)

        def open_session(self, config: Any) -> Any:
            return adapter_a.open_session(config)

    async with WorkflowRunner(
        AdapterRegistry({"no_fork": _NoForkAdapter()}),
        create_default_workflow_registry(),
    ) as no_fork_runner:
        with pytest.raises(AdapterCapabilityError, match="forking persisted sessions"):
            async for _ in no_fork_runner.run(
                lambda: _two_node_wf(),
                "No fork cap",
                tmp_path,
                adapter="no_fork",
            ):
                pass


async def _run_until_killed(workspace: Path, thread_id: str) -> None:
    if thread_id == "parallel-hard-kill":
        seen: dict[str, str] = {}
        async with _make_runner(
            "ui_question_unique", checkpoint_db=workspace / "checkpoints.sqlite"
        ) as r:
            async for event in r.run(
                _build_parallel_join_workflow,
                "Parallel crash",
                workspace,
                thread_id=thread_id,
                interaction_mode="wait",
            ):
                if isinstance(event.payload, InteractionRequestEvent) and event.node_id:
                    seen[event.node_id] = event.payload.request_id
                    if len(seen) == 2:
                        print(json.dumps(seen, sort_keys=True), flush=True)
        return
    if thread_id == "committed":
        async with _make_runner("standard", checkpoint_db=workspace / "checkpoints.sqlite") as r:
            async for event in r.run("single_agent", "Finish", workspace, thread_id=thread_id):
                if event.payload.type == "node.completed":
                    # Stop consuming. Wait until LangGraph commits before signalling the parent.
                    async with asyncio.timeout(10):
                        while True:
                            snap = await r.get_checkpoint_state("single_agent", thread_id)
                            if snap["node_outputs"].get("agent"):
                                print(snap["latest_output"], flush=True)
                                await asyncio.Event().wait()
                            await asyncio.sleep(0.01)
        return
    async with _make_runner("ui_question", checkpoint_db=workspace / "checkpoints.sqlite") as r:
        async for _ in r.run(
            "plan_and_execute",
            "Second turn",
            workspace,
            thread_id=thread_id,
            interaction_mode="wait",
        ):
            pass
        async for event in r.resume(
            "plan_and_execute",
            thread_id,
            resume_value={"decision": "approve"},
            interaction_mode="wait",
        ):
            if isinstance(event.payload, InteractionRequestEvent):
                print(event.payload.request_id, flush=True)


if __name__ == "__main__":
    asyncio.run(_run_until_killed(Path(sys.argv[1]), sys.argv[2]))
