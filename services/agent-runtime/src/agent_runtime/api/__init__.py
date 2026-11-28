"""FastAPI HTTP + SSE interface layer for CARE Agent Runtime."""

from agent_runtime.api.deps import (
    get_adapter_registry,
    get_config,
    get_run_manager,
    get_runtime_version,
    get_workflow_registry,
    require_auth,
)
from agent_runtime.api.errors import UnauthorizedError, register_exception_handlers
from agent_runtime.api.routes import api_router, health_router
from agent_runtime.api.schemas import (
    AdapterInfo,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    MessageControlRequest,
    RespondInteractionRequest,
    ResumeThreadRequest,
    StartRunRequest,
    WorkflowInfo,
)
from agent_runtime.api.sse import (
    build_sse_response,
    format_sse_event,
    patch_sse_openapi,
    resolve_after_seq,
)

__all__ = [
    "AdapterInfo",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "MessageControlRequest",
    "RespondInteractionRequest",
    "ResumeThreadRequest",
    "StartRunRequest",
    "UnauthorizedError",
    "WorkflowInfo",
    "api_router",
    "build_sse_response",
    "format_sse_event",
    "get_adapter_registry",
    "get_config",
    "get_run_manager",
    "get_runtime_version",
    "get_workflow_registry",
    "health_router",
    "patch_sse_openapi",
    "register_exception_handlers",
    "require_auth",
    "resolve_after_seq",
]
