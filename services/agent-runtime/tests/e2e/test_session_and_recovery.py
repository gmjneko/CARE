"""Production-path E2E tests for Step A (multi-turn sessions) and Step B (dual-layer recovery)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent_runtime.adapters import (
    AdapterProtocolError,
    AdapterRegistry,
    InteractionResponse,
    PiRpcAdapter,
    PiRpcConfig,
)
from agent_runtime.orchestrator import (
    AgentNodeConfig,
    RunContext,
    WorkflowRunner,
    WorkflowState,
    build_agent_node,
    continue_if_running,
)
from agent_runtime.paths import sessions_dir
from agent_runtime.protocol import (
    InteractionRequestEvent,
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
        checkpoint_db=checkpoint_db,
    )


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


def _build_three_node_workflow(
    run_context: RunContext,
    *,
    adapter: str = "pi",
    endpoint: Any = None,
    thinking_level: Any = None,
    persist_session: bool = True,
) -> StateGraph[WorkflowState]:
    """3-node workflow: planner -> executor -> reviewer (verifies downstream resume)."""
    graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
    graph.add_node(
        "planner",
        build_agent_node(
            AgentNodeConfig(
                id="planner",
                role="planner",
                adapter=adapter,
                endpoint=endpoint,
                thinking_level=thinking_level,
                persist_session=persist_session,
                tools=("read", "grep", "find", "ls"),
                append_system_prompt="You are the Planner agent.",
                prompt_builder=lambda s: f"Plan: {s['prompt']}",
            ),
            run_context,
        ),
    )
    graph.add_node(
        "executor",
        build_agent_node(
            AgentNodeConfig(
                id="executor",
                role="executor",
                adapter=adapter,
                endpoint=endpoint,
                thinking_level=thinking_level,
                persist_session=persist_session,
                prompt_builder=lambda s: f"Execute: {s['prompt']}",
            ),
            run_context,
        ),
    )
    graph.add_node(
        "reviewer",
        build_agent_node(
            AgentNodeConfig(
                id="reviewer",
                role="reviewer",
                adapter=adapter,
                endpoint=endpoint,
                thinking_level=thinking_level,
                persist_session=persist_session,
                tools=("read", "grep", "find", "ls"),
                append_system_prompt="You are the Planner agent acting as read-only reviewer.",
                prompt_builder=lambda s: f"Review: {s['latest_output']}",
            ),
            run_context,
        ),
    )
    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner", continue_if_running("executor"), {"executor": "executor", END: END}
    )
    graph.add_conditional_edges(
        "executor", continue_if_running("reviewer"), {"reviewer": "reviewer", END: END}
    )
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
        state = await runner.get_thread_state("plan_and_execute", thread_id)

    planner_ref = state.node_outputs["planner"].session_ref
    executor_ref = state.node_outputs["executor"].session_ref
    assert planner_ref is not None
    assert executor_ref is not None
    assert planner_ref != executor_ref
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
    assert interrupted.reason == "agent_interaction"
    assert interrupted.node_id == "executor"
    assert interrupted.interaction is not None
    assert interrupted.interaction.request_id == "ui-q-1"

    # Run 2 in fresh Runner (simulating recovery): planner skipped, executor resumes, reviewer runs!
    run2_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        thread_state = await runner2.get_thread_state(_build_three_node_workflow, thread_id)
        assert thread_state.status == "interrupted"
        assert thread_state.resumable is True
        assert thread_state.interrupted_node_id == "executor"
        assert thread_state.pending_interaction is not None
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
        async for ev in runner1.run(
            "plan_and_execute",
            "Choose backend",
            tmp_path,
            thread_id=thread_id,
            interaction_mode="wait",
        ):
            run1_events.append(ev)
            if isinstance(ev.payload, InteractionRequestEvent) and ev.payload.blocking:
                await runner1.suspend_all()

    assert isinstance(run1_events[-1].payload, WorkflowInterruptedEvent)
    assert run1_events[-1].payload.node_id == "executor"

    # Brand-new Runner simulates app restart
    run2_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert st.status == "interrupted"
        assert st.pending_interaction is not None
        assert st.pending_interaction.title == "Select cache backend"

        async for ev in runner2.resume(
            "plan_and_execute",
            thread_id,
            interaction_response=InteractionResponse(
                request_id=st.pending_interaction.request_id,
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
        async for ev in runner1.run(
            "plan_and_execute",
            "Refactor auth",
            tmp_path,
            thread_id=thread_id,
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
        if worker.returncode is None:
            worker.kill()
        await worker.wait()

    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert st.status == "crashed"
        assert st.pending_interaction and st.pending_interaction.request_id == request_id
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

    def _build_gated_workflow(
        run_context: RunContext,
        *,
        adapter: str = "pi",
        endpoint: Any = None,
        thinking_level: Any = None,
        persist_session: bool = True,
    ) -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "planner",
            build_agent_node(
                AgentNodeConfig(
                    id="planner",
                    role="planner",
                    adapter=adapter,
                    endpoint=endpoint,
                    thinking_level=thinking_level,
                    persist_session=persist_session,
                    tools=("read", "grep", "find", "ls"),
                    append_system_prompt="You are the Planner agent.",
                    prompt_builder=lambda s: f"Plan: {s['prompt']}",
                ),
                run_context,
            ),
        )

        def _approval_gate(state: WorkflowState) -> dict[str, Any]:
            decision = interrupt({"plan_summary": state["latest_output"]})
            if not decision.get("approved"):
                return {"status": "aborted"}
            return {"status": "running"}

        graph.add_node("approval_gate", _approval_gate)
        graph.add_node(
            "executor",
            build_agent_node(
                AgentNodeConfig(
                    id="executor",
                    role="executor",
                    adapter=adapter,
                    endpoint=endpoint,
                    thinking_level=thinking_level,
                    persist_session=persist_session,
                    prompt_builder=lambda s: f"Execute approved plan:\n{s['latest_output']}",
                ),
                run_context,
            ),
        )
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "approval_gate")
        graph.add_conditional_edges(
            "approval_gate", continue_if_running("executor"), {"executor": "executor", END: END}
        )
        graph.add_edge("executor", END)
        return graph

    run1_events: list[RuntimeEvent] = []
    async with _make_runner("standard", checkpoint_db=db_path) as runner1:
        async for ev in runner1.run(
            _build_gated_workflow,
            "Ship v2",
            tmp_path,
            thread_id=thread_id,
        ):
            run1_events.append(ev)

    interrupted = run1_events[-1].payload
    assert isinstance(interrupted, WorkflowInterruptedEvent)
    assert interrupted.reason == "workflow_interrupt"
    assert interrupted.node_id == "approval_gate"
    assert "[PLAN tools=read,grep,find,ls]" in interrupted.payload["plan_summary"]

    # Brand-new Runner resumes across process restart without re-running planner
    run2_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state(_build_gated_workflow, thread_id)
        assert st.status == "interrupted"
        assert st.resumable is True
        assert st.interrupted_node_id == "approval_gate"

        async for ev in runner2.resume(
            _build_gated_workflow,
            thread_id,
            resume_value={"approved": True},
            interaction_mode="suspend",
        ):
            run2_events.append(ev)

    pause = run2_events[-1].payload
    assert isinstance(pause, WorkflowInterruptedEvent) and pause.interaction
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner3:
        with pytest.raises(ValueError, match="request_id"):
            async for _ in runner3.resume(
                _build_gated_workflow,
                thread_id,
                interaction_response=InteractionResponse(request_id="stale", decision="deny"),
            ):
                pass
        run3_events = [
            ev
            async for ev in runner3.resume(
                _build_gated_workflow,
                thread_id,
                interaction_response=InteractionResponse(
                    request_id=pause.interaction.request_id, decision="approve", value="redis"
                ),
            )
        ]

    assert isinstance(run2_events[0].payload, WorkflowResumedEvent)
    assert run2_events[0].payload.resumed_from == "workflow_interrupt"
    started_nodes = [ev.payload.node_id for ev in run2_events if ev.payload.type == "node.started"]
    assert started_nodes == ["executor"]
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
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
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
        events1 = [
            ev
            async for ev in runner1.run(
                "plan_and_execute", "Original task", tmp_path, thread_id=thread_id
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
        events2 = [
            ev
            async for ev in runner2.run(
                "plan_and_execute", "Fresh replacement task", tmp_path, thread_id=thread_id
            )
        ]
        st2 = await runner2.get_thread_state("plan_and_execute", thread_id)

    _write_manifest(tmp_path, "fail_then_new_run", events2)
    started_nodes = [ev.payload.node_id for ev in events2 if ev.payload.type == "node.started"]
    assert started_nodes == ["planner", "executor"]
    assert isinstance(events2[-1].payload, WorkflowCompletedEvent)
    assert events2[-1].payload.status == "completed"
    assert st2.status == "completed" and st2.resumable is False
    assert st2.node_outputs["planner"].session_ref == plan_ref
    assert st2.node_outputs["executor"].session_ref == exec_ref


async def test_unresolved_interrupt_and_crash_block_run_until_discarded(tmp_path: Path) -> None:
    """Gap: unresolved pauses must reject run() until explicitly discarded, keeping session_ref."""
    db_path = tmp_path / "checkpoints.sqlite"

    # Case 1: Cold agent interaction pause (interrupted)
    thread_int = "discard-interrupted"
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner1:
        events1 = [
            ev
            async for ev in runner1.run(
                "plan_and_execute",
                "Pick backend",
                tmp_path,
                thread_id=thread_int,
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
        assert discarded.pending_interaction is None
        assert discarded.node_outputs["planner"].session_ref == plan_ref
        assert discarded.node_outputs["executor"].session_ref == exec_ref

        # Idempotent on already-closed thread, and resume() now rejects it
        assert await runner1.discard("plan_and_execute", thread_int) == discarded
        with pytest.raises(ValueError, match="already aborted"):
            await anext(runner1.resume("plan_and_execute", thread_int))

    async with _make_runner("standard", checkpoint_db=db_path) as runner2:
        events_after = [
            ev
            async for ev in runner2.run(
                "plan_and_execute", "Start fresh after discard", tmp_path, thread_id=thread_int
            )
        ]
        st_after = await runner2.get_thread_state("plan_and_execute", thread_int)

    _write_manifest(tmp_path, "discard_interrupted_then_run", events_after)
    assert isinstance(events_after[-1].payload, WorkflowCompletedEvent)
    assert events_after[-1].payload.status == "completed"
    assert st_after.node_outputs["planner"].session_ref == plan_ref
    assert st_after.node_outputs["executor"].session_ref == exec_ref

    # Case 2: Hard-killed worker crash (crashed with live WAL row)
    thread_crash = "discard-crashed"
    async with _make_runner("standard", checkpoint_db=db_path) as first:
        async for _ in first.run("plan_and_execute", "Turn 1", tmp_path, thread_id=thread_crash):
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
        if worker.returncode is None:
            worker.kill()
        await worker.wait()

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

        recovered_events = [
            ev
            async for ev in runner3.run(
                "plan_and_execute",
                "Fresh turn after crash discard",
                tmp_path,
                thread_id=thread_crash,
            )
        ]
        st_recovered = await runner3.get_thread_state("plan_and_execute", thread_crash)

    _write_manifest(tmp_path, "discard_crashed_then_run", recovered_events)
    assert isinstance(recovered_events[-1].payload, WorkflowCompletedEvent)
    assert recovered_events[-1].payload.status == "completed"
    assert st_recovered.node_outputs["executor"].session_ref == crash_exec_ref

    # Case 3: Workflow-level LangGraph interrupt() gate (paused is None, no WAL)
    def _build_gate_workflow(
        run_context: RunContext,
        *,
        adapter: str = "pi",
        endpoint: Any = None,
        thinking_level: Any = None,
        persist_session: bool = True,
    ) -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "planner",
            build_agent_node(
                AgentNodeConfig(
                    id="planner",
                    role="planner",
                    adapter=adapter,
                    endpoint=endpoint,
                    thinking_level=thinking_level,
                    persist_session=persist_session,
                    tools=("read", "grep", "find", "ls"),
                    append_system_prompt="You are the Planner agent.",
                    prompt_builder=lambda s: f"Plan: {s['prompt']}",
                ),
                run_context,
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
        assert gate_events[-1].payload.reason == "workflow_interrupt"
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
        assert closed_gate.interrupt_payload == {}
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

    def _build_wal_less_crash_workflow(
        run_context: RunContext,
        *,
        adapter: str = "pi",
        endpoint: Any = None,
        thinking_level: Any = None,
        persist_session: bool = True,
    ) -> StateGraph[WorkflowState]:
        graph: StateGraph[WorkflowState] = StateGraph(WorkflowState)
        graph.add_node(
            "planner",
            build_agent_node(
                AgentNodeConfig(
                    id="planner",
                    role="planner",
                    adapter=adapter,
                    endpoint=endpoint,
                    thinking_level=thinking_level,
                    persist_session=persist_session,
                    tools=("read", "grep", "find", "ls"),
                    append_system_prompt="You are the Planner agent.",
                    prompt_builder=lambda s: f"Plan: {s['prompt']}",
                ),
                run_context,
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


async def _run_until_killed(workspace: Path, thread_id: str) -> None:
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
        async for event in r.run(
            "plan_and_execute",
            "Second turn",
            workspace,
            thread_id=thread_id,
            interaction_mode="wait",
        ):
            if isinstance(event.payload, InteractionRequestEvent):
                print(event.payload.request_id, flush=True)


if __name__ == "__main__":
    asyncio.run(_run_until_killed(Path(sys.argv[1]), sys.argv[2]))
