"""Production-path HTTP + SSE E2E scenarios for CARE Agent Runtime's FastAPI service."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from agent_runtime.adapters import AdapterRegistry, PiRpcAdapter, PiRpcConfig
from agent_runtime.api import ServerConfig
from agent_runtime.app import create_app
from agent_runtime.config import LocalSettings, ModelProfile, save_local_settings
from agent_runtime.protocol import RuntimeEvent

FAKE_PI_SCRIPT = Path(__file__).resolve().parents[1] / "fakes" / "fake_pi_rpc.py"
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _make_fake_adapters(scenario: str) -> AdapterRegistry:
    adapter = PiRpcAdapter(
        PiRpcConfig(
            command=(sys.executable, str(FAKE_PI_SCRIPT), "--scenario", scenario),
            request_timeout_s=10.0,
        )
    )
    return AdapterRegistry({"pi": adapter})


def _make_app(
    scenario: str,
    *,
    config: ServerConfig | None = None,
) -> FastAPI:
    cfg = config if config is not None else ServerConfig(checkpoint_db=":memory:")
    return create_app(cfg, adapter_registry=_make_fake_adapters(scenario))


@contextlib.asynccontextmanager
async def _running_client(
    app: FastAPI,
    *,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Run the FastAPI app on a real ephemeral loopback uvicorn server for true HTTP/SSE E2E."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(32)
    port = sock.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=headers,
            timeout=15.0,
            trust_env=False,
        ) as client:
            async with asyncio.timeout(5.0):
                while True:
                    try:
                        h = await client.get("/healthz")
                        if h.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.02)
            yield client
    finally:
        server.should_exit = True
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(serve_task, timeout=5.0)
        sock.close()


async def _iter_sse_frames(
    response: httpx.Response,
) -> AsyncIterator[dict[str, Any]]:
    """Parse standard SSE frames (and keepalive comments) incrementally from an httpx stream."""
    event_id: str | None = None
    event_name: str | None = None
    data_lines: list[str] = []

    async for line in response.aiter_lines():
        if line == "":
            if event_id is not None or event_name is not None or data_lines:
                raw_data = "\n".join(data_lines)
                yield {
                    "id": event_id,
                    "event": event_name,
                    "data": json.loads(raw_data) if raw_data else None,
                }
                event_id = None
                event_name = None
                data_lines = []
            continue
        if line.startswith(":"):
            yield {"comment": line[1:].strip()}
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)


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


async def _wait_for_run_done(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    async with asyncio.timeout(timeout_s):
        while True:
            resp = await client.get(f"/runs/{run_id}")
            assert resp.status_code == 200
            data = resp.json()
            if data["status"] != "running":
                return data
            await asyncio.sleep(0.02)


async def test_auth_healthz_and_discovery() -> None:
    app = _make_app(
        "standard",
        config=ServerConfig(
            auth_token=SecretStr("sidecar-token-abc"),
            checkpoint_db=":memory:",
        ),
    )

    async with _running_client(app) as anon:
        health = await anon.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "mode": "local", "version": "0.0.1"}

        for path in ("/workflows", "/adapters", "/runs"):
            missing = await anon.get(path)
            assert missing.status_code == 401
            assert missing.headers.get("www-authenticate") == "Bearer"
            assert missing.json()["error"]["code"] == "unauthorized"

        wrong = await anon.get("/workflows", headers={"Authorization": "Bearer wrong-token"})
        assert wrong.status_code == 401

        auth_headers = {"Authorization": "Bearer sidecar-token-abc"}
        wf_resp = await anon.get("/workflows", headers=auth_headers)
        assert wf_resp.status_code == 200
        wf_names = {item["name"] for item in wf_resp.json()}
        assert {"single_agent", "plan_and_execute"}.issubset(wf_names)

        ad_resp = await anon.get("/adapters", headers=auth_headers)
        assert ad_resp.status_code == 200
        adapters = {item["kind"]: item["capabilities"] for item in ad_resp.json()}
        assert adapters["pi"]["streaming"] is True
        assert adapters["pi"]["steering"] is True
        assert adapters["pi"]["session_fork"] is True
        assert adapters["pi"]["interactions"] is True


async def test_start_sse_disconnect_and_reconnect_replay(tmp_path: Path) -> None:
    app = _make_app("standard", config=ServerConfig(checkpoint_db=tmp_path / "checkpoints.sqlite"))

    async with _running_client(app) as client:
        start = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Stream over SSE",
                "workspace_dir": str(tmp_path),
                "run_id": "http-run-1",
                "thread_id": "http-thread-1",
            },
        )
        assert start.status_code == 201
        snap = start.json()
        assert snap["run_id"] == "http-run-1"
        assert snap["status"] == "running"
        assert snap["last_seq"] == 1

        # Subscriber 1 disconnects right after seeing the first text.delta frame.
        part1: list[RuntimeEvent] = []
        async with client.stream("GET", "/runs/http-run-1/events") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for frame in _iter_sse_frames(resp):
                if "comment" in frame:
                    continue
                ev = RuntimeEvent.model_validate(frame["data"])
                assert frame["id"] == str(ev.seq)
                assert frame["event"] == ev.payload.type
                part1.append(ev)
                if ev.payload.type == "text.delta":
                    break

        last_seen_seq = part1[-1].seq
        done_snap = await _wait_for_run_done(client, "http-run-1")
        assert done_snap["status"] == "completed"
        assert done_snap["last_seq"] > last_seen_seq

        # Reconnect using Last-Event-ID header and also verify ?after_seq= gives identical replay.
        part2_header: list[RuntimeEvent] = []
        async with client.stream(
            "GET",
            "/runs/http-run-1/events",
            headers={"Last-Event-ID": str(last_seen_seq)},
        ) as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    part2_header.append(RuntimeEvent.model_validate(frame["data"]))

        part2_query: list[RuntimeEvent] = []
        async with client.stream(
            "GET",
            f"/runs/http-run-1/events?after_seq={last_seen_seq}",
        ) as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    part2_query.append(RuntimeEvent.model_validate(frame["data"]))

    assert [e.model_dump(mode="json") for e in part2_header] == [
        e.model_dump(mode="json") for e in part2_query
    ]
    combined = [*part1, *part2_header]
    manifest = _write_manifest(tmp_path, "http_sse_replay", combined)
    assert [ev.seq for ev in combined] == list(range(1, len(combined) + 1))
    assert manifest["events"][0]["payload"]["type"] == "workflow.started"
    assert manifest["events"][-1]["payload"]["type"] == "workflow.completed"
    assert manifest["events"][-1]["payload"]["status"] == "completed"


async def test_multi_observer_sse_and_hot_interaction_control(tmp_path: Path) -> None:
    app = _make_app(
        "ui_question",
        config=ServerConfig(checkpoint_db=tmp_path / "checkpoints.sqlite"),
    )

    async with _running_client(app) as client:
        start = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Pick cache backend",
                "workspace_dir": str(tmp_path),
                "run_id": "obs-http-1",
                "thread_id": "obs-http-thread",
            },
        )
        assert start.status_code == 201

        observer_a: list[RuntimeEvent] = []
        observer_b: list[RuntimeEvent] = []

        async def _observe_and_answer() -> None:
            async with client.stream("GET", "/runs/obs-http-1/events") as resp:
                assert resp.status_code == 200
                async for frame in _iter_sse_frames(resp):
                    if "comment" in frame:
                        continue
                    ev = RuntimeEvent.model_validate(frame["data"])
                    observer_a.append(ev)
                    if ev.payload.type == "interaction.request" and ev.payload.blocking:
                        reply = await client.post(
                            "/runs/obs-http-1/interactions",
                            json={
                                "response": {
                                    "request_id": ev.payload.request_id,
                                    "decision": "approve",
                                    "value": "redis",
                                }
                            },
                        )
                        assert reply.status_code == 204

        async def _passive_observe() -> None:
            async with client.stream("GET", "/runs/obs-http-1/events") as resp:
                assert resp.status_code == 200
                async for frame in _iter_sse_frames(resp):
                    if "comment" not in frame:
                        observer_b.append(RuntimeEvent.model_validate(frame["data"]))

        await asyncio.wait_for(
            asyncio.gather(_observe_and_answer(), _passive_observe()),
            timeout=10.0,
        )

    manifest = _write_manifest(tmp_path, "http_multi_observer", observer_a)
    assert [ev.model_dump(mode="json") for ev in observer_a] == [
        ev.model_dump(mode="json") for ev in observer_b
    ]
    assert manifest["events"][-1]["payload"]["type"] == "workflow.completed"
    assert manifest["events"][-1]["payload"]["final_output"] == "QUESTION_ANSWER=redis"


async def test_plan_and_execute_approval_gate_across_server_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"

    # Server instance 1: planner runs and stops at plan_approval interrupt.
    app1 = _make_app("standard", config=ServerConfig(checkpoint_db=db_path))
    async with _running_client(app1) as client1:
        start = await client1.post(
            "/runs",
            json={
                "workflow_id": "plan_and_execute",
                "prompt": "Refactor auth module",
                "workspace_dir": str(tmp_path),
                "run_id": "pae-run-1",
                "thread_id": "pae-thread-1",
            },
        )
        assert start.status_code == 201
        part1_events: list[RuntimeEvent] = []
        async with client1.stream("GET", "/runs/pae-run-1/events") as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    part1_events.append(RuntimeEvent.model_validate(frame["data"]))
        assert part1_events[-1].payload.type == "workflow.interrupted"

    # Server instance 2 (fresh FastAPI app + RunManager over same SQLite DB).
    app2 = _make_app("standard", config=ServerConfig(checkpoint_db=db_path))
    async with _running_client(app2) as client2:
        state_resp = await client2.get("/workflows/plan_and_execute/threads/pae-thread-1")
        assert state_resp.status_code == 200
        thread_state = state_resp.json()
        assert thread_state["status"] == "interrupted"
        assert thread_state["resumable"] is True
        assert thread_state["interrupts"][0]["node_id"] == "plan_approval"

        resume_resp = await client2.post(
            "/workflows/plan_and_execute/threads/pae-thread-1/resume",
            json={
                "run_id": "pae-run-2",
                "resume_value": {"decision": "approve", "feedback": "Ship it"},
            },
        )
        assert resume_resp.status_code == 201
        part2_events: list[RuntimeEvent] = []
        async with client2.stream("GET", "/runs/pae-run-2/events") as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    part2_events.append(RuntimeEvent.model_validate(frame["data"]))

    manifest = _write_manifest(tmp_path, "http_plan_and_execute_resume", part2_events)
    assert manifest["events"][0]["payload"]["type"] == "workflow.resumed"
    final_payload = manifest["events"][-1]["payload"]
    assert final_payload["type"] == "workflow.completed"
    assert final_payload["status"] == "completed"
    assert "received_plan=True" in final_payload["final_output"]


async def test_mid_turn_steer_abort_suspend_and_discard(tmp_path: Path) -> None:
    # 1. Mid-turn steer
    app_steer = _make_app("steerable")
    async with _running_client(app_steer) as client:
        start = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Draft response",
                "workspace_dir": str(tmp_path),
                "run_id": "steer-run-1",
            },
        )
        assert start.status_code == 201
        steer_events: list[RuntimeEvent] = []
        steered = False
        async with client.stream("GET", "/runs/steer-run-1/events") as resp:
            async for frame in _iter_sse_frames(resp):
                if "comment" in frame:
                    continue
                ev = RuntimeEvent.model_validate(frame["data"])
                steer_events.append(ev)
                if not steered and ev.payload.type == "text.delta":
                    steered = True
                    r = await client.post(
                        "/runs/steer-run-1/steer",
                        json={"message": "use postgres"},
                    )
                    assert r.status_code == 204
        assert steer_events[-1].payload.type == "workflow.completed"
        assert steer_events[-1].payload.final_output == "STEERED=use postgres"

    # 2. Mid-turn abort
    app_abort = _make_app("abortable")
    async with _running_client(app_abort) as client:
        start = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Long job",
                "workspace_dir": str(tmp_path),
                "run_id": "abort-run-1",
            },
        )
        assert start.status_code == 201
        abort_events: list[RuntimeEvent] = []
        aborted = False
        async with client.stream("GET", "/runs/abort-run-1/events") as resp:
            async for frame in _iter_sse_frames(resp):
                if "comment" in frame:
                    continue
                ev = RuntimeEvent.model_validate(frame["data"])
                abort_events.append(ev)
                if not aborted and ev.payload.type == "text.delta":
                    aborted = True
                    r = await client.post("/runs/abort-run-1/abort")
                    assert r.status_code == 204
        assert abort_events[-1].payload.type == "workflow.completed"
        assert abort_events[-1].payload.status == "aborted"
        node_completed = next(
            ev.payload for ev in abort_events if ev.payload.type == "node.completed"
        )
        assert "ABORT_ORDER=clear_queue,abort" in node_completed.summary

    # 3. Suspend hot interaction -> discard thread
    app_suspend = _make_app(
        "ui_question",
        config=ServerConfig(checkpoint_db=tmp_path / "suspend.sqlite"),
    )
    async with _running_client(app_suspend) as client:
        start = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Ask and suspend",
                "workspace_dir": str(tmp_path),
                "run_id": "susp-run-1",
                "thread_id": "susp-thread-1",
            },
        )
        assert start.status_code == 201
        susp_events: list[RuntimeEvent] = []
        suspended = False
        async with client.stream("GET", "/runs/susp-run-1/events") as resp:
            async for frame in _iter_sse_frames(resp):
                if "comment" in frame:
                    continue
                ev = RuntimeEvent.model_validate(frame["data"])
                susp_events.append(ev)
                if not suspended and ev.payload.type == "interaction.request":
                    suspended = True
                    r = await client.post("/runs/susp-run-1/suspend")
                    assert r.status_code == 204
        assert susp_events[-1].payload.type == "workflow.interrupted"

        discard_resp = await client.post("/workflows/single_agent/threads/susp-thread-1/discard")
        assert discard_resp.status_code == 200
        discarded = discard_resp.json()
        assert discarded["resumable"] is False
        assert discarded["status"] == "aborted"


async def test_local_profile_cloud_isolation_and_secret_redaction(tmp_path: Path) -> None:
    local_secret = "sk-local-super-secret-999999999999"
    save_local_settings(
        LocalSettings(
            active_profile="relay",
            profiles={
                "relay": ModelProfile(
                    model="deepseek-chat",
                    base_url="https://relay.example.com/v1",
                    api_key=SecretStr(local_secret),
                    api_protocol="openai-completions",
                    thinking_level="medium",
                )
            },
        )
    )

    # Local mode resolves profile from ~/.care/settings.json without leaking secret.
    local_app = _make_app(
        "inspect_endpoint",
        config=ServerConfig(mode="local", checkpoint_db=":memory:"),
    )
    async with _running_client(local_app) as client:
        resp = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Check endpoint",
                "workspace_dir": str(tmp_path),
                "run_id": "prof-run-1",
                "profile": "relay",
            },
        )
        assert resp.status_code == 201
        events: list[RuntimeEvent] = []
        async with client.stream("GET", "/runs/prof-run-1/events") as stream_resp:
            async for frame in _iter_sse_frames(stream_resp):
                if "comment" not in frame:
                    events.append(RuntimeEvent.model_validate(frame["data"]))
        final_ev = events[-1].payload
        assert final_ev.type == "workflow.completed"
        report = json.loads(final_ev.final_output)
        assert report["model"] == "deepseek-chat"
        assert report["thinking"] == "medium"
        assert report["env_api_key_set"] is True
        assert report["api_key_in_argv"] is False
        assert report["api_key_in_models_file"] is False
        assert local_secret not in final_ev.final_output

        # 422 validation error must never echo raw api_key back to caller.
        validation_secret = "sk-validation-secret-888888888888"
        bad_val = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Bad url",
                "workspace_dir": str(tmp_path),
                "endpoint": {
                    "model": "gpt-4o",
                    "base_url": "ftp://invalid.example.com",
                    "api_key": validation_secret,
                },
            },
        )
        assert bad_val.status_code == 422
        assert bad_val.json()["error"]["code"] == "validation_error"
        assert validation_secret not in bad_val.text

    # Cloud mode forbids profile and accepts inline ModelEndpoint.
    cloud_app = _make_app(
        "inspect_endpoint",
        config=ServerConfig(
            mode="cloud",
            auth_token=SecretStr("cloud-token"),
            checkpoint_db=":memory:",
        ),
    )
    async with _running_client(
        cloud_app, headers={"Authorization": "Bearer cloud-token"}
    ) as client:
        denied = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Cloud profile rejected",
                "workspace_dir": str(tmp_path),
                "profile": "relay",
            },
        )
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "bad_request"

        ok = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Cloud inline endpoint",
                "workspace_dir": str(tmp_path),
                "run_id": "cloud-run-1",
                "endpoint": {
                    "model": "gateway-model",
                    "base_url": "https://gateway.internal/v1",
                    "api_key": "sk-cloud-ephemeral-1234567890",
                },
            },
        )
        assert ok.status_code == 201
        cloud_events: list[RuntimeEvent] = []
        async with client.stream("GET", "/runs/cloud-run-1/events") as stream_resp:
            async for frame in _iter_sse_frames(stream_resp):
                if "comment" not in frame:
                    cloud_events.append(RuntimeEvent.model_validate(frame["data"]))
        assert cloud_events[-1].payload.type == "workflow.completed"
        cloud_report = json.loads(cloud_events[-1].payload.final_output)
        assert cloud_report["model"] == "gateway-model"
        assert cloud_report["env_api_key_set"] is True

    # Malformed stdout propagates as terminal SSE runtime.error and failed RunSnapshot.
    err_app = _make_app("malformed_stdout")
    async with _running_client(err_app) as client:
        start_err = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Trigger protocol error",
                "workspace_dir": str(tmp_path),
                "run_id": "err-http-1",
            },
        )
        assert start_err.status_code == 201
        sse_frames: list[dict[str, Any]] = []
        async with client.stream("GET", "/runs/err-http-1/events") as stream_resp:
            assert stream_resp.status_code == 200
            async for frame in _iter_sse_frames(stream_resp):
                if "comment" not in frame:
                    sse_frames.append(frame)
        assert sse_frames[-1]["event"] == "runtime.error"
        assert sse_frames[-1]["data"]["code"] == "adapter_error"
        assert "Malformed JSON" in sse_frames[-1]["data"]["message"]

        failed_snap = (await client.get("/runs/err-http-1")).json()
        assert failed_snap["status"] == "failed"
        assert "Malformed JSON" in (failed_snap["error"] or "")


async def test_http_error_codes_and_bounded_run_retention(tmp_path: Path) -> None:
    app = _make_app(
        "standard",
        config=ServerConfig(checkpoint_db=":memory:", max_retained_runs=2),
    )

    async with _running_client(app) as client:
        # 404 Not Found
        assert (await client.get("/runs/does-not-exist")).status_code == 404
        assert (await client.get("/runs/does-not-exist/events")).status_code == 404
        assert (await client.post("/runs/does-not-exist/abort")).status_code == 404
        assert (await client.post("/runs/does-not-exist/suspend")).status_code == 404
        assert (await client.get("/workflows/single_agent/threads/missing")).status_code == 404
        assert (
            await client.post(
                "/runs",
                json={
                    "workflow_id": "unknown_wf",
                    "prompt": "hi",
                    "workspace_dir": str(tmp_path),
                },
            )
        ).status_code == 404

        # 400 Bad Request
        bad_dir = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "hi",
                "workspace_dir": str(tmp_path / "does-not-exist-dir"),
            },
        )
        assert bad_dir.status_code == 400
        assert bad_dir.json()["error"]["code"] == "bad_request"

        # Run 3 completed runs -> max_retained_runs=2 evicts r1.
        for idx in (1, 2, 3):
            r = await client.post(
                "/runs",
                json={
                    "workflow_id": "single_agent",
                    "prompt": f"Run {idx}",
                    "workspace_dir": str(tmp_path),
                    "run_id": f"ret-run-{idx}",
                    "thread_id": f"ret-thread-{idx}",
                },
            )
            assert r.status_code == 201
            done = await _wait_for_run_done(client, f"ret-run-{idx}")
            assert done["status"] == "completed"

        retained_ids = [item["run_id"] for item in (await client.get("/runs")).json()]
        assert retained_ids == ["ret-run-2", "ret-run-3"]
        assert (await client.get("/runs/ret-run-1")).status_code == 404

        # Invalid Last-Event-ID on existing run -> 400
        bad_hdr = await client.get(
            "/runs/ret-run-3/events",
            headers={"Last-Event-ID": "not-an-int"},
        )
        assert bad_hdr.status_code == 400

        # 409 Conflict on finished run controls and non-resumable completed thread
        assert (await client.post("/runs/ret-run-3/abort")).status_code == 409
        assert (await client.post("/runs/ret-run-3/suspend")).status_code == 409
        assert (
            await client.post(
                "/runs/ret-run-3/interactions",
                json={"response": {"request_id": "x", "decision": "approve"}},
            )
        ).status_code == 409
        assert (
            await client.post(
                "/workflows/single_agent/threads/ret-thread-3/resume",
                json={"prompt": "again"},
            )
        ).status_code == 409

        # 409 Conflict starting a new run on an interrupted thread
        gate = await client.post(
            "/runs",
            json={
                "workflow_id": "plan_and_execute",
                "prompt": "Pause here",
                "workspace_dir": str(tmp_path),
                "run_id": "gate-run-1",
                "thread_id": "gate-thread-1",
            },
        )
        assert gate.status_code == 201
        gate_done = await _wait_for_run_done(client, "gate-run-1")
        assert gate_done["status"] == "interrupted"

        conflict_start = await client.post(
            "/runs",
            json={
                "workflow_id": "plan_and_execute",
                "prompt": "Should conflict",
                "workspace_dir": str(tmp_path),
                "thread_id": "gate-thread-1",
            },
        )
        assert conflict_start.status_code == 409
        assert conflict_start.json()["error"]["code"] == "conflict"


async def test_real_uvicorn_tcp_sse_streaming_and_keepalive(tmp_path: Path) -> None:
    """Real TCP loopback smoke test verifying incremental SSE delivery and keepalive comments."""
    app = _make_app(
        "ui_question",
        config=ServerConfig(
            auth_token=SecretStr("tcp-secret"),
            checkpoint_db=":memory:",
            sse_keepalive_s=0.15,
        ),
    )

    headers = {"Authorization": "Bearer tcp-secret"}
    async with _running_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200

        start = await client.post(
            "/runs",
            headers=headers,
            json={
                "workflow_id": "single_agent",
                "prompt": "TCP keepalive test",
                "workspace_dir": str(tmp_path),
                "run_id": "tcp-run-1",
            },
        )
        assert start.status_code == 201

        keepalives = 0
        received: list[RuntimeEvent] = []
        pending_req_id: str | None = None
        answered = False

        async with client.stream(
            "GET", "/runs/tcp-run-1/events", headers=headers
        ) as stream_resp:
            assert stream_resp.status_code == 200
            async for frame in _iter_sse_frames(stream_resp):
                if "comment" in frame:
                    keepalives += 1
                    if pending_req_id is not None and not answered:
                        answered = True
                        r = await client.post(
                            "/runs/tcp-run-1/interactions",
                            headers=headers,
                            json={
                                "response": {
                                    "request_id": pending_req_id,
                                    "decision": "approve",
                                    "value": "memory",
                                }
                            },
                        )
                        assert r.status_code == 204
                    continue
                ev = RuntimeEvent.model_validate(frame["data"])
                received.append(ev)
                if ev.payload.type == "interaction.request" and ev.payload.blocking:
                    pending_req_id = ev.payload.request_id

        assert keepalives >= 1
        assert received[-1].payload.type == "workflow.completed"
        assert received[-1].payload.final_output == "QUESTION_ANSWER=memory"


def test_canonical_event_contract_schema_is_in_sync() -> None:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import export_contracts

    schema_path = export_contracts.default_schema_path()
    assert schema_path.is_file(), f"Missing exported contract schema at {schema_path}"
    assert export_contracts.export_canonical_event_schema(schema_path, check=True)
    parsed = json.loads(schema_path.read_text(encoding="utf-8"))
    assert parsed["title"] == "RuntimeEvent"
    assert "payload" in parsed["properties"]
