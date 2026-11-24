"""Production-path E2E scenarios through WorkflowRunner + PiRpcAdapter + fake Pi subprocess."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.adapters import (
    AdapterProtocolError,
    AdapterRegistry,
    InteractionResponse,
    PiRpcAdapter,
    PiRpcConfig,
)
from agent_runtime.orchestrator import WorkflowRunner
from agent_runtime.protocol import (
    InteractionRequestEvent,
    RuntimeEvent,
    TextDeltaEvent,
    WorkflowCompletedEvent,
)
from agent_runtime.workflows import create_default_workflow_registry

FAKE_PI_SCRIPT = Path(__file__).resolve().parents[1] / "fakes" / "fake_pi_rpc.py"


def _make_runner(scenario: str, *, checkpoint_db: str | Path = ":memory:") -> WorkflowRunner:
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


def _write_manifest(run_dir: Path, events: list[RuntimeEvent]) -> dict[str, Any]:
    """Normalize runtime events into a deterministic, diff-friendly manifest."""
    normalized_events: list[dict[str, Any]] = []
    for ev in events:
        payload = ev.payload.model_dump(mode="json")
        # Strip volatile fields so two runs of the same scenario diff cleanly.
        payload.pop("session_id", None)
        normalized_events.append(
            {
                "seq": ev.seq,
                "node_id": ev.node_id,
                "is_ephemeral": ev.is_ephemeral,
                "payload": payload,
            }
        )

    manifest = {
        "workflow_id": events[0].workflow_id if events else None,
        "event_count": len(normalized_events),
        "events": normalized_events,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


async def test_single_agent_preset_streams_canonical_events_and_checkpoints(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"

    events: list[RuntimeEvent] = []
    async with _make_runner("standard", checkpoint_db=db_path) as runner:
        async for event in runner.run(
            "single_agent",
            "Build feature X",
            tmp_path,
            run_id="run-single-1",
            thread_id="thread-single-1",
        ):
            events.append(event)

        checkpoint = await runner.get_checkpoint_state("single_agent", "thread-single-1")

    manifest = _write_manifest(tmp_path, events)

    # Monotonic contiguous sequence numbers starting at 1
    assert [e["seq"] for e in manifest["events"]] == list(range(1, len(events) + 1))

    event_types = [e["payload"]["type"] for e in manifest["events"]]
    assert event_types[0] == "workflow.started"
    assert event_types[1] == "node.started"
    assert "thinking.delta" in event_types
    assert "text.delta" in event_types
    assert "tool.started" in event_types
    assert "tool.output_delta" in event_types
    assert "tool.completed" in event_types
    assert "message.completed" in event_types
    assert "turn.completed" in event_types
    assert event_types[-2] == "node.completed"
    assert event_types[-1] == "workflow.completed"

    # Cumulative tool updates were properly diffed into incremental deltas
    tool_deltas = [
        e["payload"]["chunk"]
        for e in manifest["events"]
        if e["payload"]["type"] == "tool.output_delta"
    ]
    assert tool_deltas == ["part-1\n", "part-2\n"]

    # SQLite checkpoint persists compact NodeOutput only (no streaming deltas)
    assert db_path.is_file()
    assert checkpoint["status"] == "running"
    assert set(checkpoint["node_outputs"].keys()) == {"agent"}
    assert checkpoint["node_outputs"]["agent"].status == "completed"
    assert checkpoint["node_outputs"]["agent"].usage.total_tokens == 80


async def test_plan_and_execute_preset_hands_off_structured_plan_with_tool_isolation(
    tmp_path: Path,
) -> None:
    events: list[RuntimeEvent] = []

    async with _make_runner("standard") as runner:
        async for event in runner.run(
            "plan_and_execute",
            "Implement caching",
            tmp_path,
            run_id="run-plan-exec-1",
            thread_id="thread-plan-exec-1",
        ):
            events.append(event)

        checkpoint = await runner.get_checkpoint_state("plan_and_execute", "thread-plan-exec-1")

    manifest = _write_manifest(tmp_path, events)
    assert [e["seq"] for e in manifest["events"]] == list(range(1, len(events) + 1))

    node_order = [
        e["payload"]["node_id"]
        for e in manifest["events"]
        if e["payload"]["type"] == "node.completed"
    ]
    assert node_order == ["planner", "executor"]

    planner_out = checkpoint["node_outputs"]["planner"]
    executor_out = checkpoint["node_outputs"]["executor"]
    # Planner was restricted to read-only tools and executor received planner's handoff summary
    assert "[PLAN tools=read,grep,find,ls]" in planner_out.summary
    assert "[EXEC tools=default received_plan=True]" in executor_out.summary

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == executor_out.summary


async def test_blocking_approval_does_not_deadlock_and_respects_handler(tmp_path: Path) -> None:
    seen_requests: list[InteractionRequestEvent] = []

    async def approve_handler(req: InteractionRequestEvent) -> InteractionResponse:
        seen_requests.append(req)
        return InteractionResponse(request_id=req.request_id, decision="approve")

    events: list[RuntimeEvent] = []
    async with _make_runner("ui_confirm") as runner:
        async for event in runner.run(
            "single_agent",
            "Deploy",
            tmp_path,
            interaction_handler=approve_handler,
        ):
            events.append(event)

    assert len(seen_requests) == 1
    assert seen_requests[0].kind == "approval"
    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == "UI_CONFIRM_OUTCOME=approved"


async def test_blocking_approval_without_handler_cancels_instead_of_hanging(
    tmp_path: Path,
) -> None:
    events: list[RuntimeEvent] = []

    async with _make_runner("ui_confirm") as runner:
        async for event in runner.run("single_agent", "Deploy", tmp_path):
            events.append(event)

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == "UI_CONFIRM_OUTCOME=cancelled"


async def test_mid_turn_abort_clears_queue_before_aborting(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []

    async with _make_runner("abortable") as runner:
        async for event in runner.run(
            "single_agent",
            "Long task",
            tmp_path,
            run_id="run-abort-1",
        ):
            events.append(event)
            if isinstance(event.payload, TextDeltaEvent):
                await runner.abort("run-abort-1")

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "aborted"
    assert any(
        e.payload.type == "node.completed" and "ABORT_ORDER=clear_queue,abort" in e.payload.summary
        for e in events
    )


async def test_mid_turn_steer_injects_guidance_into_active_session(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []

    async with _make_runner("steerable") as runner:
        async for event in runner.run(
            "single_agent",
            "Initial task",
            tmp_path,
            run_id="run-steer-1",
        ):
            events.append(event)
            if isinstance(event.payload, TextDeltaEvent):
                await runner.steer("run-steer-1", "switch to async")

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == "STEERED=switch to async"


async def test_planner_failure_halts_downstream_executor_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    events: list[RuntimeEvent] = []

    async with _make_runner("provider_fail_planner") as runner:
        async for event in runner.run("plan_and_execute", "Task", tmp_path):
            events.append(event)

    completed_nodes = [e.payload.node_id for e in events if e.payload.type == "node.completed"]
    assert completed_nodes == ["planner"]

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "failed"
    assert final.failure is not None
    assert final.failure.failure_class == "usage_limit"
    assert "secret_xyz_1234567890" not in final.failure.message


async def test_compaction_rejection_waits_and_retries_prompt_cleanly(tmp_path: Path) -> None:
    events: list[RuntimeEvent] = []

    async with _make_runner("compaction_race") as runner:
        async for event in runner.run("single_agent", "Retry me", tmp_path):
            events.append(event)

    compaction_phases = [e.payload.phase for e in events if e.payload.type == "compaction"]
    assert compaction_phases == ["started", "completed"]

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == "AFTER_COMPACTION:Retry me"


async def test_malformed_pi_stdout_fails_fast_with_protocol_error(tmp_path: Path) -> None:
    async with _make_runner("malformed_stdout") as runner:
        with pytest.raises(AdapterProtocolError, match="Malformed JSON"):
            async for _ in runner.run("single_agent", "Trigger noise", tmp_path):
                pass


async def test_subprocess_crash_mid_turn_surfaces_redacted_transport_failure(
    tmp_path: Path,
) -> None:
    events: list[RuntimeEvent] = []

    async with _make_runner("crash_mid_turn") as runner:
        async for event in runner.run("single_agent", "Crash now", tmp_path):
            events.append(event)

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "failed"
    assert final.failure is not None
    assert final.failure.failure_class == "transport_error"
    assert "137" in final.failure.message
    assert "sk-proj-1234567890abcdef123456" not in final.failure.message


async def test_unregistered_workflow_fails_fast(tmp_path: Path) -> None:
    async with _make_runner("standard") as runner:
        with pytest.raises(KeyError, match="Unknown workflow 'does_not_exist'"):
            async for _ in runner.run("does_not_exist", "Prompt", tmp_path):
                pass
