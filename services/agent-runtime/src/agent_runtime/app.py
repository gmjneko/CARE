"""Thin FastAPI application assembly root and `care-runtime` CLI entrypoint."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_runtime.adapters import AdapterRegistry, PiRpcAdapter, PiRpcConfig
from agent_runtime.api import (
    build_api_router,
    build_health_router,
    register_exception_handlers,
)
from agent_runtime.config import ServerConfig, resolve_named_profile, resolve_pi_bin
from agent_runtime.orchestrator import (
    PersistenceBackend,
    SqlitePersistenceBackend,
    WorkflowRegistry,
    WorkflowRunner,
)
from agent_runtime.paths import checkpoints_db_path
from agent_runtime.runtime import RunManager
from agent_runtime.workflows import create_default_workflow_registry


def create_app(
    config: ServerConfig | None = None,
    *,
    adapter_registry: AdapterRegistry | None = None,
    workflow_registry: WorkflowRegistry | None = None,
    persistence: PersistenceBackend | None = None,
) -> FastAPI:
    """Create the CARE Agent Runtime FastAPI application.

    Accepts optional production dependency seams (`adapter_registry`, `workflow_registry`,
    `persistence`) for cloud customization (e.g. PostgresPersistenceBackend) and E2E testing.
    """
    cfg = config if config is not None else ServerConfig.from_env()
    runtime_version = version("agent-runtime")

    effective_workflows = (
        workflow_registry if workflow_registry is not None else create_default_workflow_registry()
    )
    if adapter_registry is not None:
        effective_adapters = adapter_registry
    else:
        pi_bin = resolve_pi_bin(mode=cfg.mode, pi_bin=cfg.pi_bin)
        effective_adapters = AdapterRegistry({"pi": PiRpcAdapter(PiRpcConfig(command=(pi_bin,)))})

    if persistence is not None:
        effective_persistence = persistence
    else:
        db_target = cfg.checkpoint_db if cfg.checkpoint_db is not None else checkpoints_db_path()
        effective_persistence = SqlitePersistenceBackend(db_target)

    runner = WorkflowRunner(
        effective_adapters,
        effective_workflows,
        persistence=effective_persistence,
        profile_resolver=resolve_named_profile if cfg.mode == "local" else None,
    )
    manager = RunManager(runner, max_retained_runs=cfg.max_retained_runs)

    @asynccontextmanager
    async def _lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        async with manager:
            app_instance.state.run_manager = manager
            yield

    app = FastAPI(
        title="CARE Agent Runtime",
        version=runtime_version,
        lifespan=_lifespan,
    )
    app.state.config = cfg
    app.state.run_manager = manager
    app.state.workflow_registry = effective_workflows
    app.state.adapter_registry = effective_adapters

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)
    app.include_router(build_health_router(cfg, runtime_version=runtime_version))
    app.include_router(
        build_api_router(
            cfg,
            manager=manager,
            workflow_registry=effective_workflows,
            adapter_registry=effective_adapters,
        )
    )

    # FastAPI merges a default `type: "string"` for non-JSON `text/*` response classes even when
    # `responses[200]` supplies a Pydantic `model`; strip the conflicting `type` next to `$ref`.
    default_openapi = app.openapi

    def _openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = default_openapi()
        sse_schema = (
            schema.get("paths", {})
            .get("/runs/{run_id}/events", {})
            .get("get", {})
            .get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("text/event-stream", {})
            .get("schema")
        )
        if isinstance(sse_schema, dict) and "$ref" in sse_schema:
            sse_schema.pop("type", None)
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]
    return app


def cli(argv: list[str] | None = None) -> int:
    """CLI entrypoint (`care-runtime`) for starting the FastAPI server via uvicorn."""
    base = ServerConfig.from_env()
    parser = argparse.ArgumentParser(description="Run the CARE Agent Runtime HTTP + SSE server.")
    parser.add_argument("--mode", choices=("local", "cloud"), default=base.mode)
    parser.add_argument("--host", default=base.host)
    parser.add_argument("--port", type=int, default=base.port)
    parser.add_argument("--checkpoint-db", default=None)
    parser.add_argument("--pi-bin", default=base.pi_bin)
    args = parser.parse_args(argv)

    checkpoint_db = base.checkpoint_db
    if args.checkpoint_db is not None:
        trimmed = args.checkpoint_db.strip()
        checkpoint_db = ":memory:" if trimmed == ":memory:" else Path(trimmed).expanduser()

    cfg = ServerConfig(
        mode=args.mode,
        host=args.host,
        port=args.port,
        auth_token=base.auth_token,
        checkpoint_db=checkpoint_db,
        pi_bin=args.pi_bin,
        sse_keepalive_s=base.sse_keepalive_s,
        max_retained_runs=base.max_retained_runs,
        cors_origins=base.cors_origins,
    )
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
    return 0


__all__ = [
    "ServerConfig",
    "cli",
    "create_app",
]


if __name__ == "__main__":
    raise SystemExit(cli())
