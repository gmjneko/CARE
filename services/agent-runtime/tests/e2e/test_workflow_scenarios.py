"""Production-path E2E scenarios through WorkflowRunner + PiRpcAdapter + fake Pi subprocess."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.adapters import (
    AdapterProtocolError,
    AdapterRegistry,
    AdapterRpcError,
    ModelEndpoint,
    PiRpcAdapter,
    PiRpcConfig,
)
from agent_runtime.config import (
    LocalSettings,
    ModelProfile,
    load_local_settings,
    save_local_settings,
)
from agent_runtime.orchestrator import (
    SqlitePersistenceBackend,
    WorkflowRunner,
    build_approval_node,
)
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
        persistence=SqlitePersistenceBackend(checkpoint_db),
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
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
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
    db_path = tmp_path / "checkpoints.sqlite"
    thread_id = "thread-plan-exec-1"
    run1_events: list[RuntimeEvent] = []
    run2_events: list[RuntimeEvent] = []

    async with _make_runner("standard", checkpoint_db=db_path) as runner:
        async for event in runner.run(
            "plan_and_execute",
            "Implement caching",
            tmp_path,
            run_id="run-plan-exec-1",
            thread_id=thread_id,
        ):
            run1_events.append(event)

        paused = run1_events[-1].payload
        assert isinstance(paused, WorkflowInterruptedEvent)
        assert len(paused.interrupts) == 1
        gate = paused.interrupts[0]
        assert gate.kind == "workflow_interrupt"
        assert gate.node_id == "plan_approval"
        assert gate.interaction is not None
        assert gate.interaction.kind == "approval"
        assert gate.interaction.title == "确认执行计划"
        assert gate.interaction.message == "是否按照plan执行"
        assert "[PLAN tools=read,grep,find,ls]" in gate.payload["content"]

        # Invalid resume payloads must fail fast without corrupting the paused checkpoint.
        for bad_value in (
            True,
            "approve",
            {"approved": True},
            {"decision": "maybe"},
            {"decision": "approve", "feedback": 123},
        ):
            with pytest.raises(ValueError, match="plan_approval"):
                async for _ in runner.resume("plan_and_execute", thread_id, resume_value=bad_value):
                    pass
        with pytest.raises(ValueError, match="request_id"):
            async for _ in runner.resume(
                "plan_and_execute",
                thread_id,
                interaction_response=InteractionResponse(request_id="stale", decision="approve"),
            ):
                pass

        async for event in runner.resume(
            "plan_and_execute",
            thread_id,
            run_id="run-plan-exec-2",
            resume_value={"decision": "approve", "feedback": "Keep changes minimal"},
        ):
            run2_events.append(event)

        checkpoint = await runner.get_checkpoint_state("plan_and_execute", thread_id)

    manifest1 = _write_manifest(tmp_path / "run1", run1_events)
    manifest2 = _write_manifest(tmp_path / "run2", run2_events)
    assert [e["seq"] for e in manifest1["events"]] == list(range(1, len(run1_events) + 1))
    assert [e["seq"] for e in manifest2["events"]] == list(range(1, len(run2_events) + 1))

    assert [
        e["payload"]["node_id"]
        for e in manifest1["events"]
        if e["payload"]["type"] == "node.completed"
    ] == ["planner"]
    assert isinstance(run2_events[0].payload, WorkflowResumedEvent)
    assert run2_events[0].payload.resumed_from == "workflow_interrupt"
    assert [
        e["payload"]["node_id"]
        for e in manifest2["events"]
        if e["payload"]["type"] == "node.started"
    ] == ["plan_approval", "executor"]
    assert [
        e["payload"]["node_id"]
        for e in manifest2["events"]
        if e["payload"]["type"] == "node.completed"
    ] == ["plan_approval", "executor"]

    planner_out = checkpoint["node_outputs"]["planner"]
    approval_out = checkpoint["node_outputs"]["plan_approval"]
    executor_out = checkpoint["node_outputs"]["executor"]
    # Planner was restricted to read-only tools and executor received planner's handoff summary
    assert "[PLAN tools=read,grep,find,ls]" in planner_out.summary
    assert approval_out.role == "approval"
    assert approval_out.status == "completed"
    assert approval_out.summary == "approved: Keep changes minimal"
    assert "[EXEC tools=default received_plan=True]" in executor_out.summary

    final = run2_events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == executor_out.summary


async def test_plan_and_execute_approval_note_denial_and_cancel_behavior(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"

    # Construction-time validation fails fast on blank config fields.
    with pytest.raises(ValueError, match="node_id"):
        build_approval_node("  ", message="是否按照plan执行")
    with pytest.raises(ValueError, match="message"):
        build_approval_node("gate", message="  ")
    with pytest.raises(ValueError, match="title"):
        build_approval_node("gate", message="是否按照plan执行", title="   ")

    # 1. Approval feedback reaches executor prompt; denial halts before executor and preserves plan.
    async with _make_runner("stateful_chat", checkpoint_db=db_path) as runner:
        thread_approve = "plan-approve-note"
        run_approve = [
            ev
            async for ev in runner.run(
                "plan_and_execute", "Add cache", tmp_path, thread_id=thread_approve
            )
        ]
        assert isinstance(run_approve[-1].payload, WorkflowInterruptedEvent)
        approve_req = run_approve[-1].payload.interrupts[0].interaction
        assert approve_req is not None
        approved_events = [
            ev
            async for ev in runner.resume(
                "plan_and_execute",
                thread_approve,
                interaction_response=InteractionResponse(
                    request_id=approve_req.request_id,
                    decision="approve",
                    feedback="Avoid touching config files",
                ),
            )
        ]
        approved_final = approved_events[-1].payload
        assert isinstance(approved_final, WorkflowCompletedEvent)
        assert approved_final.status == "completed"
        exec_payload = json.loads(approved_final.final_output)
        assert "## Approval Note\nAvoid touching config files" in exec_payload["history"][-1]

    # 2. Denying plan_approval aborts cleanly without invoking executor and allows a fresh run.
    async with _make_runner("standard", checkpoint_db=db_path) as runner:
        thread_deny = "plan-deny"
        run1 = [
            ev
            async for ev in runner.run(
                "plan_and_execute", "Risky refactor", tmp_path, thread_id=thread_deny
            )
        ]
        assert isinstance(run1[-1].payload, WorkflowInterruptedEvent)
        plan_content = run1[-1].payload.interrupts[0].payload["content"]

        deny_events = [
            ev
            async for ev in runner.resume(
                "plan_and_execute",
                thread_deny,
                resume_value={"decision": "deny", "feedback": "Too broad"},
            )
        ]
        st_denied = await runner.get_thread_state("plan_and_execute", thread_deny)

        assert [e.payload.node_id for e in deny_events if e.payload.type == "node.started"] == [
            "plan_approval"
        ]
        assert [e.payload.node_id for e in deny_events if e.payload.type == "node.completed"] == [
            "plan_approval"
        ]
        deny_final = deny_events[-1].payload
        assert isinstance(deny_final, WorkflowCompletedEvent)
        assert deny_final.status == "aborted"
        assert deny_final.final_output == plan_content
        assert st_denied.status == "aborted"
        assert st_denied.resumable is False
        assert st_denied.node_outputs["plan_approval"].status == "aborted"
        assert st_denied.node_outputs["plan_approval"].summary == "denied: Too broad"
        assert "executor" not in st_denied.node_outputs

        # Fresh run on the same thread succeeds after denial and keeps planner's session_ref.
        planner_ref = st_denied.node_outputs["planner"].session_ref
        assert planner_ref is not None
        fresh_events = [
            ev
            async for ev in runner.run(
                "plan_and_execute", "Narrow refactor", tmp_path, thread_id=thread_deny
            )
        ]
        assert isinstance(fresh_events[-1].payload, WorkflowInterruptedEvent)
        cancel_req = fresh_events[-1].payload.interrupts[0].interaction
        assert cancel_req is not None
        cancel_events = [
            ev
            async for ev in runner.resume(
                "plan_and_execute",
                thread_deny,
                interaction_response=InteractionResponse(
                    request_id=cancel_req.request_id, decision="cancel"
                ),
            )
        ]
        st_cancelled = await runner.get_thread_state("plan_and_execute", thread_deny)

    _write_manifest(tmp_path / "denied", deny_events)
    assert isinstance(cancel_events[-1].payload, WorkflowCompletedEvent)
    assert cancel_events[-1].payload.status == "aborted"
    assert st_cancelled.node_outputs["plan_approval"].summary == "cancelled"
    assert st_cancelled.node_outputs["planner"].session_ref == planner_ref


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


async def test_abort_during_hot_interaction_dispatches_cancel_before_clear_queue_and_abort(
    tmp_path: Path,
) -> None:
    """Gap: abort() previously sent clear_queue/abort before extension_ui_response(cancel)."""
    events: list[RuntimeEvent] = []

    async with _make_runner("ui_abort_order") as runner:
        async for event in runner.run(
            "single_agent",
            "Wait on confirm",
            tmp_path,
            run_id="run-ui-abort-1",
            interaction_mode="wait",
        ):
            events.append(event)
            if isinstance(event.payload, InteractionRequestEvent) and event.payload.blocking:
                await runner.abort("run-ui-abort-1")

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "aborted"
    assert any(
        e.payload.type == "node.completed"
        and "ORDER=extension_ui_response,clear_queue,abort" in e.payload.summary
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


async def test_mid_turn_steer_rejection_surfaces_to_caller_without_breaking_turn(
    tmp_path: Path,
) -> None:
    """Gap: steer RPC rejection previously raised inside run_turn() and dropped terminal events."""
    events: list[RuntimeEvent] = []
    steer_errors: list[AdapterRpcError] = []

    async with _make_runner("steer_reject_once") as runner:
        async for event in runner.run(
            "single_agent",
            "Initial task",
            tmp_path,
            run_id="run-steer-reject-1",
            thread_id="thread-steer-reject-1",
        ):
            events.append(event)
            if isinstance(event.payload, TextDeltaEvent):
                with pytest.raises(AdapterRpcError, match="Steer rejected") as exc_info:
                    await runner.steer("run-steer-reject-1", "bad steer")
                steer_errors.append(exc_info.value)
                await runner.steer("run-steer-reject-1", "accepted steer")

    assert len(steer_errors) == 1
    assert all(ev.thread_id == "thread-steer-reject-1" for ev in events)
    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"
    assert final.final_output == "STEERED=accepted steer"


async def test_pi_rpc_config_injected_directories_override_care_home(tmp_path: Path) -> None:
    """Gap: PiRpcTransport previously hardcoded global CARE_HOME paths for sessions and tmp."""
    custom_sessions = tmp_path / "tenant-a" / "sessions"
    custom_tmp = tmp_path / "tenant-a" / "tmp"
    adapter = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", "inspect_endpoint"),
            request_timeout_s=10.0,
            sessions_dir=custom_sessions,
            tmp_dir=custom_tmp,
        )
    )
    events: list[RuntimeEvent] = []
    async with WorkflowRunner(
        AdapterRegistry({"pi": adapter}),
        create_default_workflow_registry(),
    ) as runner:
        async for ev in runner.run(
            "single_agent",
            "Check dirs",
            tmp_path,
            thread_id="tenant-a-thread",
            endpoint=ModelEndpoint(model="local-m", base_url="http://localhost:11434/v1"),
        ):
            events.append(ev)
        state = await runner.get_thread_state("single_agent", "tenant-a-thread")

    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    report = json.loads(final.final_output)
    assert Path(report["agent_dir"]).parent == custom_tmp
    assert state.node_outputs["agent"].session_ref is not None
    assert Path(state.node_outputs["agent"].session_ref).parent == custom_sessions


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


async def test_local_settings_profile_stages_isolated_pi_models_without_leaking_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    care_home = tmp_path / "care-home"
    monkeypatch.setenv("CARE_HOME", str(care_home))
    db_path = tmp_path / "checkpoints.sqlite"
    secret = "sk-care-e2e-secret-9876543210"

    saved_path = save_local_settings(
        LocalSettings(
            active_profile="relay",
            profiles={
                "relay": ModelProfile(
                    model="claude-sonnet-4-5",
                    base_url="https://relay.example.internal",
                    api_protocol="anthropic-messages",
                    api_key=secret,
                    thinking_level="high",
                )
            },
        ),
        care_home=care_home,
    )
    if os.name != "nt":
        assert stat.S_IMODE(saved_path.stat().st_mode) == 0o600

    loaded = load_local_settings(care_home=care_home)
    profile = loaded.resolve_profile()
    assert profile is not None

    events: list[RuntimeEvent] = []
    async with _make_runner("inspect_endpoint", checkpoint_db=db_path) as runner:
        async for event in runner.run(
            "single_agent",
            "Check endpoint",
            tmp_path,
            endpoint=profile.to_endpoint(),
            thinking_level=profile.thinking_level,
            run_id="run-endpoint-1",
            thread_id="thread-endpoint-1",
        ):
            events.append(event)

    manifest = _write_manifest(tmp_path, events)
    final = events[-1].payload
    assert isinstance(final, WorkflowCompletedEvent)
    assert final.status == "completed"

    report = json.loads(final.final_output)
    expected_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
    assert report["provider"] == "care"
    assert report["model"] == "claude-sonnet-4-5"
    assert report["thinking"] == "high"
    assert report["env_api_key_set"] is True
    assert report["env_api_key_sha256"] == expected_hash
    assert report["api_key_in_argv"] is False
    assert report["api_key_in_models_file"] is False
    assert report["models_json"] == {
        "providers": {
            "care": {
                "baseUrl": "https://relay.example.internal",
                "api": "anthropic-messages",
                "apiKey": "$CARE_LLM_API_KEY",
                "models": [{"id": "claude-sonnet-4-5", "reasoning": True}],
            }
        }
    }

    # Temporary isolated PI_CODING_AGENT_DIR lives under <CARE_HOME>/tmp and is cleaned up on exit.
    assert report["agent_dir"] is not None
    agent_dir_path = Path(report["agent_dir"])
    assert agent_dir_path.parent == care_home / "tmp"
    assert not agent_dir_path.exists()

    # Raw API key never leaks into events manifest or SQLite checkpoints.
    assert secret not in json.dumps(manifest)
    assert secret.encode("utf-8") not in db_path.read_bytes()


async def test_builtin_provider_key_only_and_keyless_local_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure inherited parent CARE_LLM_API_KEY does not leak into keyless local endpoints.
    monkeypatch.setenv("CARE_LLM_API_KEY", "sk-stale-parent-key")

    async with _make_runner("inspect_endpoint") as runner:
        # 1. Built-in provider with API key override only (no custom base_url)
        builtin_events: list[RuntimeEvent] = []
        async for event in runner.run(
            "single_agent",
            "Official endpoint",
            tmp_path,
            endpoint=ModelEndpoint(
                provider="openai",
                model="gpt-4o",
                api_key="sk-official-openai-key",
            ),
        ):
            builtin_events.append(event)

        # 2. Keyless custom base_url (e.g. local Ollama) without thinking_level
        local_events: list[RuntimeEvent] = []
        async for event in runner.run(
            "single_agent",
            "Local Ollama",
            tmp_path,
            endpoint=ModelEndpoint(
                model="qwen2.5-coder:7b",
                base_url="http://localhost:11434/v1",
            ),
        ):
            local_events.append(event)

    builtin_final = builtin_events[-1].payload
    assert isinstance(builtin_final, WorkflowCompletedEvent)
    builtin_report = json.loads(builtin_final.final_output)
    assert builtin_report["provider"] == "openai"
    assert builtin_report["model"] == "gpt-4o"
    assert builtin_report["models_json"] == {
        "providers": {"openai": {"apiKey": "$CARE_LLM_API_KEY"}}
    }
    assert builtin_report["env_api_key_set"] is True
    assert builtin_report["api_key_in_models_file"] is False

    local_final = local_events[-1].payload
    assert isinstance(local_final, WorkflowCompletedEvent)
    local_report = json.loads(local_final.final_output)
    assert local_report["provider"] == "care"
    assert local_report["model"] == "qwen2.5-coder:7b"
    assert local_report["env_api_key_set"] is False
    assert local_report["models_json"] == {
        "providers": {
            "care": {
                "baseUrl": "http://localhost:11434/v1",
                "api": "openai-completions",
                "apiKey": "no-key",
                "models": [{"id": "qwen2.5-coder:7b", "reasoning": False}],
            }
        }
    }
