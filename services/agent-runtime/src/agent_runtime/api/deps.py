"""FastAPI dependency providers and Bearer authentication security dependency."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agent_runtime.adapters import AdapterRegistry
from agent_runtime.api.errors import UnauthorizedError
from agent_runtime.config import ServerConfig
from agent_runtime.orchestrator import WorkflowRegistry
from agent_runtime.runtime import RunManager

_bearer_scheme = HTTPBearer(auto_error=False)


def get_config(request: Request) -> ServerConfig:
    """Return the ServerConfig bound to the active FastAPI application."""
    return request.app.state.config


def get_run_manager(request: Request) -> RunManager:
    """Return the RunManager bound to the active FastAPI application."""
    return request.app.state.run_manager


def get_workflow_registry(request: Request) -> WorkflowRegistry:
    """Return the WorkflowRegistry bound to the active FastAPI application."""
    return request.app.state.workflow_registry


def get_adapter_registry(request: Request) -> AdapterRegistry:
    """Return the AdapterRegistry bound to the active FastAPI application."""
    return request.app.state.adapter_registry


def get_runtime_version(request: Request) -> str:
    """Return the FastAPI application version string."""
    return request.app.version


ConfigDep = Annotated[ServerConfig, Depends(get_config)]
RunManagerDep = Annotated[RunManager, Depends(get_run_manager)]
WorkflowRegistryDep = Annotated[WorkflowRegistry, Depends(get_workflow_registry)]
AdapterRegistryDep = Annotated[AdapterRegistry, Depends(get_adapter_registry)]
RuntimeVersionDep = Annotated[str, Depends(get_runtime_version)]


def require_auth(
    request: Request,
    config: ConfigDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)] = None,
) -> None:
    """Enforce constant-time Bearer token auth when `ServerConfig.auth_token` is configured."""
    expected = config.auth_token
    if expected is None:
        return
    if not request.headers.get("authorization"):
        raise UnauthorizedError("Missing Authorization header")
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not credentials.credentials
        or not hmac.compare_digest(credentials.credentials, expected.get_secret_value())
    ):
        raise UnauthorizedError("Invalid bearer token")


__all__ = [
    "AdapterRegistryDep",
    "ConfigDep",
    "RunManagerDep",
    "RuntimeVersionDep",
    "WorkflowRegistryDep",
    "get_adapter_registry",
    "get_config",
    "get_run_manager",
    "get_runtime_version",
    "get_workflow_registry",
    "require_auth",
]
