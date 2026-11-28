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
from agent_runtime.app import create_app
from agent_runtime.config import ServerConfig
from agent_runtime.protocol import RuntimeEvent
from agent_runtime.settings import LocalSettings, ModelProfile, save_local_settings

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

        for path in ("/workflows", "/adapters", "/runs", "/threads", "/settings"):
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


async def test_plan_and_execute_interactive_handoff_across_server_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite"

    # Server instance 1: planner runs and pauses in awaiting_input for interactive refinement.
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
        assert thread_state["interrupts"][0]["node_id"] == "planner"
        assert thread_state["interrupts"][0]["kind"] == "awaiting_input"

        # Complete planner -> executor forks planner session, runs, and pauses in awaiting_input.
        resume_resp = await client2.post(
            "/workflows/plan_and_execute/threads/pae-thread-1/resume",
            json={
                "run_id": "pae-run-2",
                "complete": True,
            },
        )
        assert resume_resp.status_code == 201
        part2_events: list[RuntimeEvent] = []
        async with client2.stream("GET", "/runs/pae-run-2/events") as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    part2_events.append(RuntimeEvent.model_validate(frame["data"]))
        assert part2_events[-1].payload.type == "workflow.interrupted"
        assert part2_events[-1].payload.interrupts[0].node_id == "executor"
        assert part2_events[-1].payload.interrupts[0].kind == "awaiting_input"

        # Complete executor -> workflow completes with executor output.
        done_resp = await client2.post(
            "/workflows/plan_and_execute/threads/pae-thread-1/resume",
            json={
                "run_id": "pae-run-3",
                "complete": True,
            },
        )
        assert done_resp.status_code == 201
        part3_events: list[RuntimeEvent] = []
        async with client2.stream("GET", "/runs/pae-run-3/events") as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    part3_events.append(RuntimeEvent.model_validate(frame["data"]))

    manifest = _write_manifest(
        tmp_path, "http_plan_and_execute_resume", [*part2_events, *part3_events]
    )
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

    # Malformed stdout propagates as canonical RuntimeEvent(runtime.error) and failed RunSnapshot.
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
        last_frame = sse_frames[-1]
        assert last_frame["event"] == "runtime.error"
        err_event = RuntimeEvent.model_validate(last_frame["data"])
        assert last_frame["id"] == str(err_event.seq)
        assert last_frame["data"]["is_ephemeral"] is False
        assert err_event.run_id == "err-http-1"
        assert err_event.payload.type == "runtime.error"
        assert err_event.payload.code == "adapter_error"
        assert "Malformed JSON" in err_event.payload.message

        failed_snap = (await client.get("/runs/err-http-1")).json()
        assert failed_snap["status"] == "failed"
        assert "Malformed JSON" in (failed_snap["error"] or "")


async def test_http_error_codes_and_bounded_run_retention(tmp_path: Path) -> None:
    app = _make_app(
        "standard",
        config=ServerConfig(checkpoint_db=":memory:", max_retained_runs=2),
    )

    async with _running_client(app) as client:
        # 404 Not Found & 405 Method Not Allowed from FastAPI routing use unified ErrorResponse.
        unknown_route = await client.get("/does-not-exist-route")
        assert unknown_route.status_code == 404
        assert unknown_route.json()["error"]["code"] == "not_found"

        wrong_method = await client.delete("/runs")
        assert wrong_method.status_code == 405
        assert wrong_method.json()["error"]["code"] == "method_not_allowed"
        assert "allow" in wrong_method.headers

        # 404 Not Found on domain resources
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
    raw_text = schema_path.read_text(encoding="utf-8")
    parsed = json.loads(raw_text)
    assert parsed["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert "$defs" not in parsed
    assert '"const"' not in raw_text
    assert parsed["title"] == "RuntimeEvent"
    assert "is_ephemeral" in parsed["properties"]
    assert "is_ephemeral" in parsed["required"]
    assert parsed["properties"]["seq"]["minimum"] == 1
    assert "RuntimeErrorEvent" in parsed["definitions"]
    assert parsed["properties"]["payload"]["discriminator"]["mapping"]["runtime.error"] == (
        "#/definitions/RuntimeErrorEvent"
    )
    assert None in parsed["definitions"]["InteractionRequestEvent"]["properties"]["input_mode"][
        "enum"
    ]

    # OpenAPI declares text/event-stream referencing canonical RuntimeEvent (with runtime.error).
    openapi = _make_app("standard").openapi()
    sse_200 = openapi["paths"]["/runs/{run_id}/events"]["get"]["responses"]["200"]
    assert list(sse_200["content"].keys()) == ["text/event-stream"]
    assert sse_200["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/RuntimeEvent"
    }
    assert "RuntimeErrorEvent" in openapi["components"]["schemas"]
    assert openapi["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }


async def test_unexpected_internal_key_and_value_errors_surface_as_sanitized_500(
    caplog: Any,
) -> None:
    """Gap: global KeyError/ValueError handlers previously masked internal bugs as 404/400
    and leaked raw exception text without server-side logging."""
    app = _make_app("standard")

    @app.get("/_test_internal_key_bug")
    async def _bug_key() -> dict[str, str]:
        raise KeyError("secret_internal_dict_key")

    @app.get("/_test_internal_value_bug")
    async def _bug_val() -> dict[str, str]:
        raise ValueError("internal_parse_detail_xyz")

    caplog.clear()
    async with _running_client(app) as client:
        for path, leaked in (
            ("/_test_internal_key_bug", "secret_internal_dict_key"),
            ("/_test_internal_value_bug", "internal_parse_detail_xyz"),
        ):
            resp = await client.get(path)
            assert resp.status_code == 500
            body = resp.json()
            assert body == {
                "error": {"code": "internal_error", "message": "Internal server error"}
            }
            assert leaked not in resp.text

    assert "secret_internal_dict_key" in caplog.text
    assert "internal_parse_detail_xyz" in caplog.text


async def test_thread_workflow_ownership_and_live_thread_state(tmp_path: Path) -> None:
    """Gap 1: threads shared one checkpointer namespace without verifying workflow_id ownership.
    Gap 3: online get_thread_state() reused crash reconciliation and labeled live nodes crashed."""
    db_path = tmp_path / "ownership.sqlite"

    # 1. Offline cross-workflow ownership checks across get/resume/discard/start_run.
    app_pae = _make_app("standard", config=ServerConfig(checkpoint_db=db_path))
    async with _running_client(app_pae) as client:
        start = await client.post(
            "/runs",
            json={
                "workflow_id": "plan_and_execute",
                "prompt": "Plan ownership check",
                "workspace_dir": str(tmp_path),
                "run_id": "own-run-1",
                "thread_id": "own-thread-1",
            },
        )
        assert start.status_code == 201
        done = await _wait_for_run_done(client, "own-run-1")
        assert done["status"] == "interrupted"

        for method, url, payload in (
            ("GET", "/workflows/single_agent/threads/own-thread-1", None),
            (
                "POST",
                "/workflows/single_agent/threads/own-thread-1/resume",
                {"prompt": "wrong wf"},
            ),
            ("POST", "/workflows/single_agent/threads/own-thread-1/discard", None),
            (
                "POST",
                "/runs",
                {
                    "workflow_id": "single_agent",
                    "prompt": "steal thread",
                    "workspace_dir": str(tmp_path),
                    "thread_id": "own-thread-1",
                },
            ),
        ):
            resp = await client.request(method, url, json=payload)
            assert resp.status_code == 409, f"{method} {url} -> {resp.status_code}: {resp.text}"
            assert resp.json()["error"]["code"] == "conflict"

        # True owner can discard cleanly, and ownership remains enforced afterward.
        discard_ok = await client.post("/workflows/plan_and_execute/threads/own-thread-1/discard")
        assert discard_ok.status_code == 200
        assert (
            await client.get("/workflows/single_agent/threads/own-thread-1")
        ).status_code == 409

    # 2. Online thread state reports live agent_interaction (never crashed) + active_run_id.
    app_live = _make_app("ui_question", config=ServerConfig(checkpoint_db=tmp_path / "live.sqlite"))
    async with _running_client(app_live) as client:
        start_live = await client.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Ask cache backend",
                "workspace_dir": str(tmp_path),
                "run_id": "live-run-1",
                "thread_id": "live-thread-1",
            },
        )
        assert start_live.status_code == 201

        live_state: dict[str, Any] | None = None
        async with client.stream("GET", "/runs/live-run-1/events") as stream_resp:
            assert stream_resp.status_code == 200
            async for frame in _iter_sse_frames(stream_resp):
                if "comment" in frame:
                    continue
                ev = RuntimeEvent.model_validate(frame["data"])
                if ev.payload.type == "interaction.request" and ev.payload.blocking:
                    # Cross-workflow query on live thread is also rejected with 409.
                    wrong_live = await client.get(
                        "/workflows/plan_and_execute/threads/live-thread-1"
                    )
                    assert wrong_live.status_code == 409

                    state_resp = await client.get("/workflows/single_agent/threads/live-thread-1")
                    assert state_resp.status_code == 200
                    live_state = state_resp.json()
                    assert live_state["status"] == "running"
                    assert live_state["resumable"] is False
                    assert live_state["active_run_id"] == "live-run-1"
                    assert live_state["inflight_nodes"] == []
                    assert len(live_state["interrupts"]) == 1
                    assert live_state["interrupts"][0]["kind"] == "agent_interaction"
                    assert live_state["interrupts"][0]["node_id"] == "agent"
                    assert (
                        live_state["interrupts"][0]["interaction"]["request_id"]
                        == ev.payload.request_id
                    )
                    assert "agent" not in live_state["node_outputs"]

                    # Reply using active_run_id from live ThreadState.
                    reply = await client.post(
                        f"/runs/{live_state['active_run_id']}/interactions",
                        json={
                            "response": {
                                "request_id": ev.payload.request_id,
                                "decision": "approve",
                                "value": "redis",
                            }
                        },
                    )
                    assert reply.status_code == 204

        assert live_state is not None
        final_state = (await client.get("/workflows/single_agent/threads/live-thread-1")).json()
        assert final_state["status"] == "completed"
        assert final_state["active_run_id"] is None
        assert final_state["interrupts"] == []


async def test_resume_inherits_and_persists_execution_config_across_restart(
    tmp_path: Path,
) -> None:
    """Gap 2: resuming a paused turn previously reset model/thinking/node_overrides to defaults.

    Verifies A -> pause -> inherit A -> override B -> pause -> restart -> prompt-only uses B
    by inspecting the actual config received by the fake Pi RPC subprocess, plus cloud inline
    endpoint in-process retention and cross-restart fail-fast without persisting api_key.
    """
    secret_a = "sk-profile-a-secret-111111111111"
    secret_b = "sk-profile-b-secret-222222222222"
    save_local_settings(
        LocalSettings(
            active_profile="profile_a",
            profiles={
                "profile_a": ModelProfile(
                    model="model-a",
                    base_url="https://a.example.com/v1",
                    api_key=SecretStr(secret_a),
                    thinking_level="low",
                ),
                "profile_b": ModelProfile(
                    model="model-b",
                    base_url="https://b.example.com/v1",
                    api_key=SecretStr(secret_b),
                    thinking_level="high",
                ),
            },
        )
    )

    async def _collect_interrupted_summary(
        client: httpx.AsyncClient, run_id: str
    ) -> dict[str, Any]:
        events: list[RuntimeEvent] = []
        async with client.stream("GET", f"/runs/{run_id}/events") as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    events.append(RuntimeEvent.model_validate(frame["data"]))
        last = events[-1].payload
        assert last.type == "workflow.interrupted"
        return json.loads(last.interrupts[0].payload["summary"])

    db_local = tmp_path / "resume_local.sqlite"
    app1 = _make_app("inspect_endpoint", config=ServerConfig(mode="local", checkpoint_db=db_local))
    async with _running_client(app1) as client1:
        # 1. Start with profile_a + planner read_only override -> pauses in awaiting_input.
        r1 = await client1.post(
            "/runs",
            json={
                "workflow_id": "plan_and_execute",
                "prompt": "Initial plan",
                "workspace_dir": str(tmp_path),
                "run_id": "cfg-run-1",
                "thread_id": "cfg-thread-1",
                "profile": "profile_a",
                "node_overrides": {"planner": {"tool_policy": "read_only"}},
            },
        )
        assert r1.status_code == 201
        rep1 = await _collect_interrupted_summary(client1, "cfg-run-1")
        assert rep1["model"] == "model-a"
        assert rep1["thinking"] == "low"
        assert rep1["tools"] == "read,grep,find,ls"
        assert rep1["env_api_key_set"] is True

        # 2. Resume with prompt only -> inherits profile_a + planner read_only override.
        r2 = await client1.post(
            "/workflows/plan_and_execute/threads/cfg-thread-1/resume",
            json={"run_id": "cfg-run-2", "prompt": "Refine with A"},
        )
        assert r2.status_code == 201
        rep2 = await _collect_interrupted_summary(client1, "cfg-run-2")
        assert rep2["model"] == "model-a"
        assert rep2["thinking"] == "low"
        assert rep2["tools"] == "read,grep,find,ls"
        assert rep2["env_api_key_sha256"] == rep1["env_api_key_sha256"]

        # 3. Override to profile_b + explicit planner tools -> pauses again.
        r3 = await client1.post(
            "/workflows/plan_and_execute/threads/cfg-thread-1/resume",
            json={
                "run_id": "cfg-run-3",
                "prompt": "Switch to B",
                "profile": "profile_b",
                "node_overrides": {"planner": {"tools": ["read", "grep"]}},
            },
        )
        assert r3.status_code == 201
        rep3 = await _collect_interrupted_summary(client1, "cfg-run-3")
        assert rep3["model"] == "model-b"
        assert rep3["thinking"] == "high"
        assert rep3["tools"] == "read,grep"
        assert rep3["env_api_key_set"] is True
        assert rep3["env_api_key_sha256"] != rep1["env_api_key_sha256"]

    # Ensure no API key ever touched SQLite on disk.
    raw_local_db = db_local.read_bytes()
    assert secret_a.encode() not in raw_local_db
    assert secret_b.encode() not in raw_local_db

    # 4. Simulate server restart (fresh app over same SQLite DB), send ONLY prompt -> must use B!
    app2 = _make_app("inspect_endpoint", config=ServerConfig(mode="local", checkpoint_db=db_local))
    async with _running_client(app2) as client2:
        r4 = await client2.post(
            "/workflows/plan_and_execute/threads/cfg-thread-1/resume",
            json={"run_id": "cfg-run-4", "prompt": "Continue after restart"},
        )
        assert r4.status_code == 201
        rep4 = await _collect_interrupted_summary(client2, "cfg-run-4")
        assert rep4["model"] == "model-b"
        assert rep4["thinking"] == "high"
        assert rep4["tools"] == "read,grep"
        assert rep4["env_api_key_sha256"] == rep3["env_api_key_sha256"]

        # Complete planner -> executor inherits profile_b as well.
        r5 = await client2.post(
            "/workflows/plan_and_execute/threads/cfg-thread-1/resume",
            json={"run_id": "cfg-run-5", "complete": True},
        )
        assert r5.status_code == 201
        rep5 = await _collect_interrupted_summary(client2, "cfg-run-5")
        assert rep5["model"] == "model-b"
        assert rep5["thinking"] == "high"
        assert rep5["env_api_key_sha256"] == rep3["env_api_key_sha256"]

    # 5. Cloud inline endpoint with api_key: retained in-process, fails fast after restart.
    db_cloud = tmp_path / "resume_cloud.sqlite"
    cloud_secret = "sk-cloud-inline-secret-99999999"
    cloud_headers = {"Authorization": "Bearer cloud-tok"}
    cloud_app1 = _make_app(
        "inspect_endpoint",
        config=ServerConfig(
            mode="cloud",
            auth_token=SecretStr("cloud-tok"),
            checkpoint_db=db_cloud,
        ),
    )
    async with _running_client(cloud_app1, headers=cloud_headers) as c1:
        cr1 = await c1.post(
            "/runs",
            json={
                "workflow_id": "plan_and_execute",
                "prompt": "Cloud start",
                "workspace_dir": str(tmp_path),
                "run_id": "cloud-cfg-1",
                "thread_id": "cloud-cfg-thread",
                "endpoint": {
                    "model": "cloud-m1",
                    "base_url": "https://gateway.internal/v1",
                    "api_key": cloud_secret,
                },
                "thinking_level": "medium",
            },
        )
        assert cr1.status_code == 201
        crep1 = await _collect_interrupted_summary(c1, "cloud-cfg-1")
        assert crep1["model"] == "cloud-m1"
        assert crep1["thinking"] == "medium"
        assert crep1["env_api_key_set"] is True

        # Same-process resume with prompt only reuses cached in-memory secret cleanly.
        cr2 = await c1.post(
            "/workflows/plan_and_execute/threads/cloud-cfg-thread/resume",
            json={"run_id": "cloud-cfg-2", "prompt": "Cloud follow-up"},
        )
        assert cr2.status_code == 201
        crep2 = await _collect_interrupted_summary(c1, "cloud-cfg-2")
        assert crep2["model"] == "cloud-m1"
        assert crep2["thinking"] == "medium"
        assert crep2["env_api_key_sha256"] == crep1["env_api_key_sha256"]

    assert cloud_secret.encode() not in db_cloud.read_bytes()

    # Cross-restart cloud resume without endpoint fails fast (400) rather than dropping to None.
    cloud_app2 = _make_app(
        "inspect_endpoint",
        config=ServerConfig(
            mode="cloud",
            auth_token=SecretStr("cloud-tok"),
            checkpoint_db=db_cloud,
        ),
    )
    async with _running_client(cloud_app2, headers=cloud_headers) as c2:
        missing_key = await c2.post(
            "/workflows/plan_and_execute/threads/cloud-cfg-thread/resume",
            json={"run_id": "cloud-cfg-3", "prompt": "After restart without key"},
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["error"]["code"] == "bad_request"
        assert "api_key" in missing_key.json()["error"]["message"]

        resumed_with_ep = await c2.post(
            "/workflows/plan_and_execute/threads/cloud-cfg-thread/resume",
            json={
                "run_id": "cloud-cfg-4",
                "prompt": "After restart with key",
                "endpoint": {
                    "model": "cloud-m2",
                    "base_url": "https://gateway.internal/v1",
                    "api_key": "sk-cloud-refreshed-88888888",
                },
            },
        )
        assert resumed_with_ep.status_code == 201
        crep4 = await _collect_interrupted_summary(c2, "cloud-cfg-4")
        assert crep4["model"] == "cloud-m2"
        assert crep4["env_api_key_set"] is True


async def test_thread_history_listing_replay_workspace_filter_and_crash_reconciliation(
    tmp_path: Path,
) -> None:
    """E2E: GET /threads and GET /threads/{id}/events across multi-turn, restart, and crash."""
    ws_a = tmp_path / "workspace-a"
    ws_b = tmp_path / "workspace-b"
    ws_a.mkdir()
    ws_b.mkdir()
    db_path = tmp_path / "history.sqlite"

    async def _stream_all(client: httpx.AsyncClient, run_id: str) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        async with client.stream("GET", f"/runs/{run_id}/events") as resp:
            assert resp.status_code == 200
            async for frame in _iter_sse_frames(resp):
                if "comment" not in frame:
                    events.append(RuntimeEvent.model_validate(frame["data"]))
        return events

    # 1. Run multi-turn single_agent in ws_a, paused plan_and_execute in ws_a, and thread in ws_b.
    app1 = _make_app("standard", config=ServerConfig(checkpoint_db=db_path))
    async with _running_client(app1) as client1:
        r1 = await client1.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "First turn title\nwith extra details",
                "workspace_dir": str(ws_a),
                "run_id": "hist-run-1",
                "thread_id": "hist-thread-1",
            },
        )
        assert r1.status_code == 201
        sse_turn1 = await _stream_all(client1, "hist-run-1")

        r2 = await client1.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Second turn follow-up",
                "workspace_dir": str(ws_a),
                "run_id": "hist-run-2",
                "thread_id": "hist-thread-1",
            },
        )
        assert r2.status_code == 201
        sse_turn2 = await _stream_all(client1, "hist-run-2")

        r3 = await client1.post(
            "/runs",
            json={
                "workflow_id": "plan_and_execute",
                "prompt": "Plan API schema",
                "workspace_dir": str(ws_a),
                "run_id": "hist-run-3",
                "thread_id": "hist-thread-2",
            },
        )
        assert r3.status_code == 201
        await _wait_for_run_done(client1, "hist-run-3")

        r4 = await client1.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Workspace B task",
                "workspace_dir": str(ws_b),
                "run_id": "hist-run-4",
                "thread_id": "hist-thread-3",
            },
        )
        assert r4.status_code == 201
        await _wait_for_run_done(client1, "hist-run-4")

    # 2. Simulate full server restart over the same SQLite DB.
    app2 = _make_app("standard", config=ServerConfig(checkpoint_db=db_path))
    async with _running_client(app2) as client2:
        all_resp = await client2.get("/threads")
        assert all_resp.status_code == 200
        threads = all_resp.json()
        assert [t["thread_id"] for t in threads] == [
            "hist-thread-3",
            "hist-thread-2",
            "hist-thread-1",
        ]
        by_id = {t["thread_id"]: t for t in threads}
        assert by_id["hist-thread-1"]["title"] == "First turn title"
        assert by_id["hist-thread-1"]["status"] == "completed"
        assert by_id["hist-thread-1"]["last_run_id"] == "hist-run-2"
        assert by_id["hist-thread-1"]["active_run_id"] is None
        assert by_id["hist-thread-2"]["status"] == "interrupted"
        assert by_id["hist-thread-2"]["workflow_id"] == "plan_and_execute"

        ws_a_resp = await client2.get("/threads", params={"workspace_dir": str(ws_a)})
        assert ws_a_resp.status_code == 200
        assert [t["thread_id"] for t in ws_a_resp.json()] == ["hist-thread-2", "hist-thread-1"]

        limited_resp = await client2.get("/threads", params={"limit": 1})
        assert limited_resp.status_code == 200
        assert [t["thread_id"] for t in limited_resp.json()] == ["hist-thread-3"]

        # Durable event history matches non-ephemeral SSE events across Turn 1 + Turn 2.
        events_resp = await client2.get("/threads/hist-thread-1/events")
        assert events_resp.status_code == 200
        persisted_events = [RuntimeEvent.model_validate(item) for item in events_resp.json()]
        assert all(not ev.is_ephemeral for ev in persisted_events)
        expected_durable = [ev for ev in (*sse_turn1, *sse_turn2) if not ev.is_ephemeral]
        assert [
            (ev.run_id, ev.seq, ev.node_id, ev.payload.type) for ev in persisted_events
        ] == [
            (ev.run_id, ev.seq, ev.node_id, ev.payload.type) for ev in expected_durable
        ]
        manifest = _write_manifest(tmp_path, "thread_history", persisted_events)
        assert manifest["workflow_id"] == "single_agent"
        assert manifest["event_count"] == len(expected_durable)

        missing = await client2.get("/threads/does-not-exist/events")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"

    # 3. Live active run reports status='running' + active_run_id on GET /threads.
    app_live = _make_app("ui_question", config=ServerConfig(checkpoint_db=db_path))
    async with _running_client(app_live) as client_live:
        live_start = await client_live.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Live thread listing check",
                "workspace_dir": str(ws_a),
                "run_id": "hist-live-run",
                "thread_id": "hist-live-thread",
            },
        )
        assert live_start.status_code == 201
        async with client_live.stream("GET", "/runs/hist-live-run/events") as stream_resp:
            async for frame in _iter_sse_frames(stream_resp):
                if "comment" in frame:
                    continue
                ev = RuntimeEvent.model_validate(frame["data"])
                if ev.payload.type == "interaction.request" and ev.payload.blocking:
                    live_threads = {
                        item["thread_id"]: item
                        for item in (await client_live.get("/threads")).json()
                    }
                    assert live_threads["hist-live-thread"]["status"] == "running"
                    assert live_threads["hist-live-thread"]["active_run_id"] == "hist-live-run"
                    await client_live.post(
                        "/runs/hist-live-run/interactions",
                        json={
                            "response": {
                                "request_id": ev.payload.request_id,
                                "decision": "approve",
                                "value": "sqlite",
                            }
                        },
                    )

    # 4. Unhandled AdapterError mid-turn reconciles to status='crashed' in both GET /threads and
    # GET /workflows/{wf}/threads/{tid}, records runtime.error, and discard() flips it to 'aborted'.
    app_crash = _make_app("malformed_stdout", config=ServerConfig(checkpoint_db=db_path))
    async with _running_client(app_crash) as client_crash:
        crash_start = await client_crash.post(
            "/runs",
            json={
                "workflow_id": "single_agent",
                "prompt": "Crash mid turn",
                "workspace_dir": str(ws_a),
                "run_id": "hist-crash-run",
                "thread_id": "hist-crash-thread",
            },
        )
        assert crash_start.status_code == 201
        crash_done = await _wait_for_run_done(client_crash, "hist-crash-run")
        assert crash_done["status"] == "failed"

        crashed_state = (
            await client_crash.get("/workflows/single_agent/threads/hist-crash-thread")
        ).json()
        assert crashed_state["status"] == "crashed"

        crashed_threads = {
            item["thread_id"]: item for item in (await client_crash.get("/threads")).json()
        }
        assert crashed_threads["hist-crash-thread"]["status"] == "crashed"
        assert crashed_threads["hist-crash-thread"]["active_run_id"] is None

        crash_events = [
            RuntimeEvent.model_validate(item)
            for item in (await client_crash.get("/threads/hist-crash-thread/events")).json()
        ]
        assert crash_events[-1].payload.type == "runtime.error"
        assert crash_events[-1].payload.code == "adapter_error"

        discard_resp = await client_crash.post(
            "/workflows/single_agent/threads/hist-crash-thread/discard"
        )
        assert discard_resp.status_code == 200
        after_discard = {
            item["thread_id"]: item for item in (await client_crash.get("/threads")).json()
        }
        assert after_discard["hist-crash-thread"]["status"] == "aborted"


async def test_local_settings_api_redaction_key_preservation_and_cloud_guard(
    tmp_path: Path,
) -> None:
    """E2E: GET/PUT /settings redacts secrets, preserves omitted keys, and blocks cloud mode."""
    secret_1 = "sk-settings-initial-secret-11111111"
    secret_2 = "sk-settings-rotated-secret-22222222"

    app = _make_app("inspect_endpoint", config=ServerConfig(mode="local", checkpoint_db=":memory:"))
    async with _running_client(app) as client:
        # 1. Empty default settings on fresh CARE_HOME.
        initial = await client.get("/settings")
        assert initial.status_code == 200
        assert initial.json() == {"active_profile": None, "profiles": {}, "adapter_bins": {}}

        # 2. Save profiles via PUT /settings -> response is masked and never leaks api_key.
        put1 = await client.put(
            "/settings",
            json={
                "active_profile": "primary",
                "profiles": {
                    "primary": {
                        "model": "gpt-4o",
                        "base_url": "https://gateway.example.com/v1",
                        "api_key": secret_1,
                        "thinking_level": "low",
                    },
                    "ollama": {
                        "model": "qwen2.5-coder:7b",
                        "base_url": "http://localhost:11434/v1",
                    },
                },
                "adapter_bins": {"pi": "/opt/homebrew/bin/pi"},
            },
        )
        assert put1.status_code == 200
        assert secret_1 not in put1.text
        body1 = put1.json()
        assert body1["active_profile"] == "primary"
        assert body1["adapter_bins"] == {"pi": "/opt/homebrew/bin/pi"}
        assert body1["profiles"]["primary"] == {
            "model": "gpt-4o",
            "provider": None,
            "base_url": "https://gateway.example.com/v1",
            "api_protocol": "openai-completions",
            "thinking_level": "low",
            "has_api_key": True,
        }
        assert body1["profiles"]["ollama"]["has_api_key"] is False

        get1 = await client.get("/settings")
        assert get1.status_code == 200
        assert secret_1 not in get1.text
        assert get1.json() == body1

        async def _inspect_profile_run(run_id: str, profile: str) -> dict[str, Any]:
            r = await client.post(
                "/runs",
                json={
                    "workflow_id": "single_agent",
                    "prompt": "Inspect endpoint",
                    "workspace_dir": str(tmp_path),
                    "run_id": run_id,
                    "profile": profile,
                },
            )
            assert r.status_code == 201
            events: list[RuntimeEvent] = []
            async with client.stream("GET", f"/runs/{run_id}/events") as resp:
                async for frame in _iter_sse_frames(resp):
                    if "comment" not in frame:
                        events.append(RuntimeEvent.model_validate(frame["data"]))
            last = events[-1].payload
            assert last.type == "workflow.completed"
            return json.loads(last.final_output)

        rep1 = await _inspect_profile_run("set-run-1", "primary")
        assert rep1["model"] == "gpt-4o"
        assert rep1["thinking"] == "low"
        assert rep1["env_api_key_set"] is True

        # 3. Omitting api_key preserves the stored secret while updating thinking_level.
        put2 = await client.put(
            "/settings",
            json={
                "active_profile": "primary",
                "profiles": {
                    "primary": {
                        "model": "gpt-4o",
                        "base_url": "https://gateway.example.com/v1",
                        "thinking_level": "high",
                    },
                },
                "adapter_bins": {"pi": "/opt/homebrew/bin/pi"},
            },
        )
        assert put2.status_code == 200
        assert secret_1 not in put2.text
        assert put2.json()["profiles"]["primary"]["has_api_key"] is True
        assert put2.json()["profiles"]["primary"]["thinking_level"] == "high"
        assert "ollama" not in put2.json()["profiles"]

        rep2 = await _inspect_profile_run("set-run-2", "primary")
        assert rep2["thinking"] == "high"
        assert rep2["env_api_key_sha256"] == rep1["env_api_key_sha256"]

        # 4. Omitting api_key while dropping base_url & provider rejects invalid merged profile
        # without leaking the stored secret.
        bad_merge = await client.put(
            "/settings",
            json={
                "active_profile": "primary",
                "profiles": {
                    "primary": {"model": "gpt-4o"},
                },
            },
        )
        assert bad_merge.status_code == 400
        assert bad_merge.json()["error"]["code"] == "bad_request"
        assert secret_1 not in bad_merge.text

        # 5. Rotating api_key updates the secret used by subsequent runs.
        put3 = await client.put(
            "/settings",
            json={
                "active_profile": "primary",
                "profiles": {
                    "primary": {
                        "model": "gpt-4o",
                        "base_url": "https://gateway.example.com/v1",
                        "api_key": secret_2,
                    },
                },
            },
        )
        assert put3.status_code == 200
        assert secret_2 not in put3.text
        assert put3.json()["profiles"]["primary"]["has_api_key"] is True

        rep3 = await _inspect_profile_run("set-run-3", "primary")
        assert rep3["env_api_key_set"] is True
        assert rep3["env_api_key_sha256"] != rep1["env_api_key_sha256"]

        # 6. Explicitly sending api_key=null clears the stored secret.
        put4 = await client.put(
            "/settings",
            json={
                "active_profile": "primary",
                "profiles": {
                    "primary": {
                        "model": "gpt-4o",
                        "base_url": "http://localhost:11434/v1",
                        "api_key": None,
                    },
                },
            },
        )
        assert put4.status_code == 200
        assert put4.json()["profiles"]["primary"]["has_api_key"] is False

        rep4 = await _inspect_profile_run("set-run-4", "primary")
        assert rep4["env_api_key_set"] is False

        # 7. Validation errors strip submitted secrets and return 422.
        leaked_candidate = "sk-never-echo-me-99999999"
        invalid = await client.put(
            "/settings",
            json={
                "active_profile": "missing",
                "profiles": {
                    "primary": {
                        "model": "gpt-4o",
                        "base_url": "https://gateway.example.com/v1",
                        "api_key": leaked_candidate,
                    }
                },
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"
        assert leaked_candidate not in invalid.text

    # 8. Cloud mode rejects both GET /settings and PUT /settings with 400.
    cloud_headers = {"Authorization": "Bearer cloud-secret"}
    cloud_app = _make_app(
        "standard",
        config=ServerConfig(
            mode="cloud",
            auth_token=SecretStr("cloud-secret"),
            checkpoint_db=":memory:",
        ),
    )
    async with _running_client(cloud_app, headers=cloud_headers) as cloud_client:
        c_get = await cloud_client.get("/settings")
        assert c_get.status_code == 400
        assert c_get.json()["error"]["code"] == "bad_request"

        c_put = await cloud_client.put("/settings", json={"profiles": {}})
        assert c_put.status_code == 400
        assert c_put.json()["error"]["code"] == "bad_request"
