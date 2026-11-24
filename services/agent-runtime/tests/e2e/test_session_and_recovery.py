"""Production-path E2E tests for Step A (multi-turn sessions) and Step B (dual-layer recovery)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent_runtime.adapters import (
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

    # Step 1: Run planner to completion, then simulate an unrecovered hard crash inside executor
    # after WAL recorded executor's session_ref + pending_interaction (before executor returns).
    sess_file = sessions_dir() / "wal-crashed-exec.jsonl"
    sess_file.parent.mkdir(parents=True, exist_ok=True)
    sess_file.write_text(json.dumps({"prompt": "original executor turn"}) + "\n", encoding="utf-8")

    pending_req = InteractionRequestEvent(
        request_id="wal-req-1",
        kind="user_input",
        blocking=True,
        title="Confirm target env",
        message="Staging or prod?",
        options=("staging", "prod"),
    )

    def _build_crashing_workflow(
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

        async def _crashing_executor(state: WorkflowState) -> dict[str, Any]:
            assert run_context.inflight_store is not None
            await run_context.inflight_store.upsert(
                state["thread_id"],
                "executor",
                session_ref=str(sess_file),
                pending_interaction=pending_req,
            )
            raise RuntimeError("simulated kill -9 before checkpoint commit")

        graph.add_node("executor", _crashing_executor)
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "executor")
        graph.add_edge("executor", END)
        return graph

    async with _make_runner("standard", checkpoint_db=db_path) as runner1:
        with pytest.raises(RuntimeError, match="simulated kill -9"):
            async for _ in runner1.run(
                _build_crashing_workflow,
                "Deploy service",
                tmp_path,
                thread_id=thread_id,
            ):
                pass

    # Step 2: Brand-new Runner with real plan_and_execute recovers from SQLite checkpoint + WAL!
    resumed_events: list[RuntimeEvent] = []
    async with _make_runner("ui_question", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state("plan_and_execute", thread_id)
        assert st.status == "crashed"
        assert st.resumable is True
        assert st.interrupted_node_id == "executor"
        assert st.pending_interaction == pending_req
        assert st.node_outputs["executor"].session_ref == str(sess_file)

        async for ev in runner2.resume(
            "plan_and_execute",
            thread_id,
            interaction_response=InteractionResponse(
                request_id="wal-req-1",
                decision="approve",
                value="staging",
            ),
        ):
            resumed_events.append(ev)

        after = await runner2.get_thread_state("plan_and_execute", thread_id)

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
    async with _make_runner("standard", checkpoint_db=db_path) as runner2:
        st = await runner2.get_thread_state(_build_gated_workflow, thread_id)
        assert st.status == "interrupted"
        assert st.resumable is True
        assert st.interrupted_node_id == "approval_gate"

        async for ev in runner2.resume(
            _build_gated_workflow,
            thread_id,
            resume_value={"approved": True},
        ):
            run2_events.append(ev)

    assert isinstance(run2_events[0].payload, WorkflowResumedEvent)
    assert run2_events[0].payload.resumed_from == "workflow_interrupt"
    started_nodes = [ev.payload.node_id for ev in run2_events if ev.payload.type == "node.started"]
    assert started_nodes == ["executor"]
    final = run2_events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert "[EXEC tools=default received_plan=True]" in final.final_output
