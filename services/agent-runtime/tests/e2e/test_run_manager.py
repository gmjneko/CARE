"""Production-path E2E scenarios for application-layer RunManager."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.adapters import AdapterProtocolError, AdapterRegistry, PiRpcAdapter, PiRpcConfig
from agent_runtime.orchestrator import SqlitePersistenceBackend, WorkflowRunner
from agent_runtime.protocol import (
    InteractionRequestEvent,
    InteractionResponse,
    RuntimeEvent,
    TextDeltaEvent,
    WorkflowCompletedEvent,
    WorkflowInterruptedEvent,
)
from agent_runtime.runtime import RunManager
from agent_runtime.workflows import create_default_workflow_registry

FAKE_PI_SCRIPT = Path(__file__).resolve().parents[1] / "fakes" / "fake_pi_rpc.py"


def _make_manager(scenario: str, *, checkpoint_db: str | Path = ":memory:") -> RunManager:
    adapter = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", scenario),
            request_timeout_s=10.0,
        )
    )
    runner = WorkflowRunner(
        AdapterRegistry({"pi": adapter}),
        create_default_workflow_registry(),
        persistence=SqlitePersistenceBackend(checkpoint_db),
    )
    return RunManager(runner)


def _write_manifest(run_dir: Path, name: str, events: list[RuntimeEvent]) -> dict[str, Any]:
    normalized = []
    for ev in events:
        payload = ev.payload.model_dump(mode="json")
        payload.pop("session_id", None)
        normalized.append(
            {
                "seq": ev.seq,
                "thread_id": ev.thread_id,
                "node_id": ev.node_id,
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


async def test_client_disconnect_does_not_stop_background_run_and_after_seq_replays_cleanly(
    tmp_path: Path,
) -> None:
    """Gap: single-consumer AsyncIterator previously stopped execution on client disconnect."""
    db_path = tmp_path / "checkpoints.sqlite"

    async with _make_manager("standard", checkpoint_db=db_path) as manager:
        snap = await manager.start_run(
            "single_agent",
            "Run in background",
            tmp_path,
            run_id="bg-run-1",
            thread_id="bg-thread-1",
        )
        assert snap.status == "running"
        assert snap.last_seq == 1

        # Subscriber 1 disconnects right after seeing the first streaming text delta.
        part1: list[RuntimeEvent] = []
        async for ev in manager.subscribe(snap.run_id):
            part1.append(ev)
            if isinstance(ev.payload, TextDeltaEvent):
                break
        last_seen_seq = part1[-1].seq

        # Background run keeps executing and reaches completion without any active subscriber.
        done_snap = await manager.wait(snap.run_id)
        assert done_snap.status == "completed"
        assert done_snap.last_seq > last_seen_seq

        # Subscriber 2 reconnects with after_seq and catches up every missed event seamlessly.
        part2 = [ev async for ev in manager.subscribe(snap.run_id, after_seq=last_seen_seq)]

    combined = [*part1, *part2]
    manifest = _write_manifest(tmp_path, "reconnect_replay", combined)
    assert [ev.seq for ev in combined] == list(range(1, len(combined) + 1))
    assert all(ev.thread_id == "bg-thread-1" for ev in combined)
    assert manifest["events"][0]["payload"]["type"] == "workflow.started"
    assert isinstance(combined[-1].payload, WorkflowCompletedEvent)
    assert combined[-1].payload.status == "completed"


async def test_multi_subscriber_observe_and_remote_interaction_control(tmp_path: Path) -> None:
    """Concurrent observers receive identical event streams while another client responds."""
    db_path = tmp_path / "checkpoints.sqlite"

    async with _make_manager("ui_question", checkpoint_db=db_path) as manager:
        snap = await manager.start_run(
            "single_agent",
            "Pick cache",
            tmp_path,
            run_id="obs-run-1",
            thread_id="obs-thread-1",
            interaction_mode="wait",
        )

        observer_a: list[RuntimeEvent] = []
        observer_b: list[RuntimeEvent] = []

        async def _observe_and_answer() -> None:
            async for ev in manager.subscribe(snap.run_id):
                observer_a.append(ev)
                if isinstance(ev.payload, InteractionRequestEvent) and ev.payload.blocking:
                    await manager.respond_interaction(
                        snap.run_id,
                        InteractionResponse(
                            request_id=ev.payload.request_id,
                            decision="approve",
                            value="redis",
                        ),
                    )

        async def _passive_observe() -> None:
            async for ev in manager.subscribe(snap.run_id):
                observer_b.append(ev)

        await asyncio.wait_for(
            asyncio.gather(_observe_and_answer(), _passive_observe()),
            timeout=10.0,
        )
        final_snap = manager.get_run(snap.run_id)

    _write_manifest(tmp_path, "multi_observer", observer_a)
    assert final_snap.status == "completed"
    assert [ev.model_dump(mode="json") for ev in observer_a] == [
        ev.model_dump(mode="json") for ev in observer_b
    ]
    assert isinstance(observer_a[-1].payload, WorkflowCompletedEvent)
    assert observer_a[-1].payload.final_output == "QUESTION_ANSWER=redis"


async def test_run_manager_preflight_validation_and_background_exception_propagation(
    tmp_path: Path,
) -> None:
    """Invalid start/resume fails fast synchronously; runtime errors surface to subscribers/wait."""
    async with _make_manager("standard") as manager:
        with pytest.raises(KeyError, match="Unknown workflow 'nope'"):
            await manager.start_run("nope", "Hello", tmp_path)

        # Start plan_and_execute -> pauses at planner(awaiting_input); invalid resume fails fast.
        s1 = await manager.start_run("plan_and_execute", "Check", tmp_path, thread_id="rm-gate")
        done1 = await manager.wait(s1.run_id)
        assert done1.status == "interrupted"
        ev1 = [ev async for ev in manager.subscribe(s1.run_id)]
        assert isinstance(ev1[-1].payload, WorkflowInterruptedEvent)

        with pytest.raises(ValueError, match="planner"):
            await manager.start_resume("plan_and_execute", "rm-gate", resume_value={"bad": True})

        s2 = await manager.start_resume("plan_and_execute", "rm-gate", complete=True)
        done2 = await manager.wait(s2.run_id)
        assert done2.status == "interrupted"

        s3 = await manager.start_resume("plan_and_execute", "rm-gate", complete=True)
        done3 = await manager.wait(s3.run_id)
        assert done3.status == "completed"

    # Unhandled protocol error in background task propagates to both subscribe() and wait().
    async with _make_manager("malformed_stdout") as manager_err:
        err_snap = await manager_err.start_run(
            "single_agent", "Noise", tmp_path, run_id="err-run-1"
        )
        with pytest.raises(AdapterProtocolError, match="Malformed JSON"):
            async for _ in manager_err.subscribe(err_snap.run_id):
                pass
        with pytest.raises(AdapterProtocolError, match="Malformed JSON"):
            await manager_err.wait(err_snap.run_id)
        failed_snap = manager_err.get_run(err_snap.run_id)
        assert failed_snap.status == "failed"
        assert failed_snap.error is not None and "Malformed JSON" in failed_snap.error
