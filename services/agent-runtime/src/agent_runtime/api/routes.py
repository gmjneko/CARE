"""FastAPI routes and Bearer token auth dependency over RunManager."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from agent_runtime.adapters import AdapterRegistry
from agent_runtime.api.errors import UnauthorizedError
from agent_runtime.api.schemas import (
    AdapterInfo,
    HealthResponse,
    MessageControlRequest,
    RespondInteractionRequest,
    ResumeThreadRequest,
    StartRunRequest,
    WorkflowInfo,
)
from agent_runtime.api.sse import EventStreamResponse, build_sse_response, resolve_after_seq
from agent_runtime.config import ServerConfig, resolve_run_model
from agent_runtime.errors import InvalidInputError
from agent_runtime.orchestrator import ThreadState, WorkflowRegistry
from agent_runtime.protocol import RuntimeEvent
from agent_runtime.runtime import RunManager, RunSnapshot
from agent_runtime.settings import load_local_settings


def build_health_router(config: ServerConfig, *, runtime_version: str) -> APIRouter:
    """Create the unauthenticated health probe router."""
    router = APIRouter()

    @router.get("/healthz", response_model=HealthResponse, tags=["system"])
    async def healthz() -> HealthResponse:
        return HealthResponse(mode=config.mode, version=runtime_version)

    return router


def build_api_router(
    config: ServerConfig,
    *,
    manager: RunManager,
    workflow_registry: WorkflowRegistry,
    adapter_registry: AdapterRegistry,
) -> APIRouter:
    """Create the Bearer-protected REST + SSE router for discovery, runs, and threads."""

    def _require_auth(request: Request) -> None:
        expected = config.auth_token
        if expected is None:
            return
        header = request.headers.get("authorization")
        if not header:
            raise UnauthorizedError("Missing Authorization header")
        scheme, _, token = header.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not token
            or not hmac.compare_digest(token, expected.get_secret_value())
        ):
            raise UnauthorizedError("Invalid bearer token")

    api = APIRouter(dependencies=[Depends(_require_auth)])

    @api.get("/workflows", response_model=list[WorkflowInfo], tags=["discovery"])
    async def list_workflows() -> list[WorkflowInfo]:
        return [
            WorkflowInfo(name=defn.name, description=defn.description)
            for defn in workflow_registry.list_definitions()
        ]

    @api.get("/adapters", response_model=list[AdapterInfo], tags=["discovery"])
    async def list_adapters() -> list[AdapterInfo]:
        return [
            AdapterInfo(kind=kind, capabilities=adapter_registry.get(kind).capabilities)
            for kind in adapter_registry.list_kinds()
        ]

    @api.post(
        "/runs",
        response_model=RunSnapshot,
        status_code=status.HTTP_201_CREATED,
        tags=["runs"],
    )
    async def start_run(body: StartRunRequest) -> RunSnapshot:
        workspace = Path(body.workspace_dir).expanduser()
        if not workspace.is_dir():
            raise InvalidInputError(
                f"workspace_dir does not exist or is not a directory: '{body.workspace_dir}'"
            )
        effective_endpoint, effective_thinking = resolve_run_model(
            mode=config.mode,
            profile=body.profile,
            endpoint=body.endpoint,
            thinking_level=body.thinking_level,
        )
        effective_profile: str | None = None
        if config.mode == "local" and body.endpoint is None:
            effective_profile = (
                body.profile.strip()
                if body.profile is not None and body.profile.strip()
                else load_local_settings().active_profile
            )
        return await manager.start_run(
            body.workflow_id.strip(),
            body.prompt.strip(),
            workspace,
            run_id=body.run_id,
            thread_id=body.thread_id,
            adapter=body.adapter,
            profile=effective_profile,
            endpoint=effective_endpoint,
            thinking_level=effective_thinking,
            persist_session=body.persist_session,
            node_overrides=body.node_overrides or None,
            interaction_mode=body.interaction_mode,
        )

    @api.get("/runs", response_model=list[RunSnapshot], tags=["runs"])
    async def list_runs() -> list[RunSnapshot]:
        return list(manager.list_runs())

    @api.get("/runs/{run_id}", response_model=RunSnapshot, tags=["runs"])
    async def get_run(run_id: str) -> RunSnapshot:
        return manager.get_run(run_id)

    _SSE_RESPONSES: dict[int | str, dict[str, Any]] = {
        200: {
            "description": (
                "Server-Sent Events stream of canonical RuntimeEvent frames "
                "(id=seq, event=payload.type, data=RuntimeEvent JSON)."
            ),
            "model": RuntimeEvent,
        }
    }

    @api.get(
        "/runs/{run_id}/events",
        response_class=EventStreamResponse,
        responses=_SSE_RESPONSES,
        tags=["runs"],
    )
    async def stream_run_events(
        run_id: str,
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

    @api.post(
        "/runs/{run_id}/interactions",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["runs"],
    )
    async def respond_interaction(run_id: str, body: RespondInteractionRequest) -> Response:
        await manager.respond_interaction(run_id, body.response, node_id=body.node_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/runs/{run_id}/steer",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["runs"],
    )
    async def steer_run(run_id: str, body: MessageControlRequest) -> Response:
        await manager.steer(run_id, body.message.strip(), node_id=body.node_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/runs/{run_id}/follow-up",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["runs"],
    )
    async def follow_up_run(run_id: str, body: MessageControlRequest) -> Response:
        await manager.follow_up(run_id, body.message.strip(), node_id=body.node_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/runs/{run_id}/abort",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["runs"],
    )
    async def abort_run(run_id: str) -> Response:
        await manager.abort(run_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/runs/{run_id}/suspend",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["runs"],
    )
    async def suspend_run(run_id: str) -> Response:
        await manager.suspend(run_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/workflows/{workflow_id}/threads/{thread_id}",
        response_model=ThreadState,
        tags=["threads"],
    )
    async def get_thread_state(workflow_id: str, thread_id: str) -> ThreadState:
        return await manager.get_thread_state(workflow_id, thread_id)

    @api.post(
        "/workflows/{workflow_id}/threads/{thread_id}/resume",
        response_model=RunSnapshot,
        status_code=status.HTTP_201_CREATED,
        tags=["threads"],
    )
    async def resume_thread(
        workflow_id: str,
        thread_id: str,
        body: ResumeThreadRequest,
    ) -> RunSnapshot:
        effective_profile: str | None = None
        effective_endpoint = None
        effective_thinking = body.thinking_level
        if body.profile is not None or body.endpoint is not None:
            effective_endpoint, effective_thinking = resolve_run_model(
                mode=config.mode,
                profile=body.profile,
                endpoint=body.endpoint,
                thinking_level=body.thinking_level,
            )
            if body.profile is not None and body.profile.strip():
                effective_profile = body.profile.strip()
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
            profile=effective_profile,
            endpoint=effective_endpoint,
            thinking_level=effective_thinking,
            persist_session=body.persist_session,
            node_overrides=body.node_overrides,
            interaction_mode=body.interaction_mode,
        )

    @api.post(
        "/workflows/{workflow_id}/threads/{thread_id}/discard",
        response_model=ThreadState,
        tags=["threads"],
    )
    async def discard_thread(workflow_id: str, thread_id: str) -> ThreadState:
        return await manager.discard(workflow_id, thread_id)

    return api


__all__ = [
    "build_api_router",
    "build_health_router",
]
