"""FastAPI HTTP + SSE routes over RunManager and runtime registries."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from agent_runtime.api.deps import (
    AdapterRegistryDep,
    ConfigDep,
    RunManagerDep,
    RuntimeVersionDep,
    SettingsLockDep,
    WorkflowRegistryDep,
    require_auth,
)
from agent_runtime.api.errors import format_validation_error
from agent_runtime.api.schemas import (
    AdapterInfo,
    HealthResponse,
    LocalSettingsResponse,
    MessageControlRequest,
    RespondInteractionRequest,
    ResumeThreadRequest,
    StartRunRequest,
    WorkflowInfo,
)
from agent_runtime.api.sse import EventStreamResponse, build_sse_response, resolve_after_seq
from agent_runtime.config import resolve_run_model
from agent_runtime.errors import InvalidInputError
from agent_runtime.orchestrator import ThreadState, ThreadSummary
from agent_runtime.protocol import RuntimeEvent
from agent_runtime.runtime import RunSnapshot
from agent_runtime.settings import (
    LocalSettings,
    ModelProfile,
    load_local_settings,
    save_local_settings,
)

health_router = APIRouter(tags=["system"])
api_router = APIRouter(dependencies=[Depends(require_auth)])

_SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "Server-Sent Events stream of canonical RuntimeEvent frames "
            "(id=seq, event=payload.type, data=RuntimeEvent JSON)."
        ),
        "model": RuntimeEvent,
    }
}


@health_router.get("/healthz", response_model=HealthResponse)
async def healthz(config: ConfigDep, runtime_version: RuntimeVersionDep) -> HealthResponse:
    return HealthResponse(mode=config.mode, version=runtime_version)


@api_router.get("/workflows", response_model=list[WorkflowInfo], tags=["discovery"])
async def list_workflows(workflow_registry: WorkflowRegistryDep) -> list[WorkflowInfo]:
    return [
        WorkflowInfo(name=defn.name, description=defn.description)
        for defn in workflow_registry.list_definitions()
    ]


@api_router.get("/adapters", response_model=list[AdapterInfo], tags=["discovery"])
async def list_adapters(adapter_registry: AdapterRegistryDep) -> list[AdapterInfo]:
    return [
        AdapterInfo(kind=kind, capabilities=adapter_registry.get(kind).capabilities)
        for kind in adapter_registry.list_kinds()
    ]


@api_router.post(
    "/runs",
    response_model=RunSnapshot,
    status_code=status.HTTP_201_CREATED,
    tags=["runs"],
)
async def start_run(
    body: StartRunRequest,
    config: ConfigDep,
    manager: RunManagerDep,
) -> RunSnapshot:
    resolved = resolve_run_model(
        mode=config.mode,
        profile=body.profile,
        endpoint=body.endpoint,
        thinking_level=body.thinking_level,
    )
    return await manager.start_run(
        body.workflow_id,
        body.prompt,
        body.workspace_dir,
        run_id=body.run_id,
        thread_id=body.thread_id,
        adapter=body.adapter,
        profile=resolved.profile,
        endpoint=resolved.endpoint,
        thinking_level=resolved.thinking_level,
        persist_session=body.persist_session,
        node_overrides=body.node_overrides or None,
        interaction_mode=body.interaction_mode,
    )


@api_router.get("/runs", response_model=list[RunSnapshot], tags=["runs"])
async def list_runs(manager: RunManagerDep) -> list[RunSnapshot]:
    return list(manager.list_runs())


@api_router.get("/runs/{run_id}", response_model=RunSnapshot, tags=["runs"])
async def get_run(run_id: str, manager: RunManagerDep) -> RunSnapshot:
    return manager.get_run(run_id)


@api_router.get(
    "/runs/{run_id}/events",
    response_class=EventStreamResponse,
    responses=_SSE_RESPONSES,
    tags=["runs"],
)
async def stream_run_events(
    run_id: str,
    config: ConfigDep,
    manager: RunManagerDep,
    after_seq: int | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    start_seq = resolve_after_seq(after_seq, last_event_id)
    # Atomically validate run existence and pin an active subscriber lease before opening SSE.
    subscription = manager.subscribe(run_id, after_seq=start_seq)
    try:
        return build_sse_response(
            subscription,
            keepalive_s=config.sse_keepalive_s,
        )
    except BaseException:
        await subscription.aclose()
        raise


@api_router.post(
    "/runs/{run_id}/interactions",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["runs"],
)
async def respond_interaction(
    run_id: str,
    body: RespondInteractionRequest,
    manager: RunManagerDep,
) -> Response:
    await manager.respond_interaction(run_id, body.response, node_id=body.node_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.post(
    "/runs/{run_id}/steer",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["runs"],
)
async def steer_run(
    run_id: str,
    body: MessageControlRequest,
    manager: RunManagerDep,
) -> Response:
    await manager.steer(run_id, body.message, node_id=body.node_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.post(
    "/runs/{run_id}/follow-up",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["runs"],
)
async def follow_up_run(
    run_id: str,
    body: MessageControlRequest,
    manager: RunManagerDep,
) -> Response:
    await manager.follow_up(run_id, body.message, node_id=body.node_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.post(
    "/runs/{run_id}/abort",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["runs"],
)
async def abort_run(run_id: str, manager: RunManagerDep) -> Response:
    await manager.abort(run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.post(
    "/runs/{run_id}/suspend",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["runs"],
)
async def suspend_run(run_id: str, manager: RunManagerDep) -> Response:
    await manager.suspend(run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api_router.get("/threads", response_model=list[ThreadSummary], tags=["threads"])
async def list_threads(
    manager: RunManagerDep,
    workspace_dir: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ThreadSummary]:
    return list(await manager.list_threads(workspace_dir=workspace_dir, limit=limit))


@api_router.get(
    "/threads/{thread_id}/events",
    response_model=list[RuntimeEvent],
    tags=["threads"],
)
async def get_thread_events(thread_id: str, manager: RunManagerDep) -> list[RuntimeEvent]:
    return list(await manager.get_thread_events(thread_id))


@api_router.get(
    "/workflows/{workflow_id}/threads/{thread_id}",
    response_model=ThreadState,
    tags=["threads"],
)
async def get_thread_state(
    workflow_id: str,
    thread_id: str,
    manager: RunManagerDep,
) -> ThreadState:
    return await manager.get_thread_state(workflow_id, thread_id)


@api_router.post(
    "/workflows/{workflow_id}/threads/{thread_id}/resume",
    response_model=RunSnapshot,
    status_code=status.HTTP_201_CREATED,
    tags=["threads"],
)
async def resume_thread(
    workflow_id: str,
    thread_id: str,
    body: ResumeThreadRequest,
    manager: RunManagerDep,
) -> RunSnapshot:
    return await manager.start_resume(
        workflow_id,
        thread_id,
        run_id=body.run_id,
        node_id=body.node_id,
        resume_value=body.resume_value,
        interaction_response=body.interaction_response,
        prompt=body.prompt,
        complete=body.complete,
        resumes=body.resumes,
        adapter=body.adapter,
        profile=body.profile,
        endpoint=body.endpoint,
        thinking_level=body.thinking_level,
        persist_session=body.persist_session,
        node_overrides=body.node_overrides,
        interaction_mode=body.interaction_mode,
    )


@api_router.post(
    "/workflows/{workflow_id}/threads/{thread_id}/discard",
    response_model=ThreadState,
    tags=["threads"],
)
async def discard_thread(
    workflow_id: str,
    thread_id: str,
    manager: RunManagerDep,
) -> ThreadState:
    return await manager.discard(workflow_id, thread_id)


def _require_local_mode(config: ConfigDep) -> None:
    if config.mode != "local":
        raise InvalidInputError("Local settings API is only supported in local mode")


@api_router.get("/settings", response_model=LocalSettingsResponse, tags=["settings"])
async def get_settings(config: ConfigDep) -> LocalSettingsResponse:
    _require_local_mode(config)
    return LocalSettingsResponse.from_settings(load_local_settings())


@api_router.put("/settings", response_model=LocalSettingsResponse, tags=["settings"])
async def update_settings(
    body: LocalSettings,
    config: ConfigDep,
    settings_lock: SettingsLockDep,
) -> LocalSettingsResponse:
    _require_local_mode(config)
    async with settings_lock:
        existing = load_local_settings()
        merged_profiles: dict[str, ModelProfile] = {}
        try:
            for name, incoming in body.profiles.items():
                if "api_key" not in incoming.model_fields_set and name in existing.profiles:
                    preserved_key = existing.profiles[name].api_key
                    if preserved_key is not None:
                        merged_profiles[name] = ModelProfile(
                            model=incoming.model,
                            provider=incoming.provider,
                            base_url=incoming.base_url,
                            api_protocol=incoming.api_protocol,
                            api_key=preserved_key,
                            thinking_level=incoming.thinking_level,
                        )
                        continue
                merged_profiles[name] = incoming
            updated = LocalSettings(
                active_profile=body.active_profile,
                profiles=merged_profiles,
                adapter_bins=body.adapter_bins,
            )
        except ValidationError as exc:
            raise InvalidInputError(format_validation_error(exc)) from exc
        save_local_settings(updated)
    return LocalSettingsResponse.from_settings(updated)


__all__ = [
    "api_router",
    "health_router",
]
