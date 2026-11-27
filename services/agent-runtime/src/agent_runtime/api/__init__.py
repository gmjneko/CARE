"""FastAPI HTTP + SSE interface layer for CARE Agent Runtime."""

from agent_runtime.api.config import (
    RuntimeMode,
    ServerConfig,
    resolve_default_pi_bin,
    resolve_run_model,
)
from agent_runtime.api.errors import UnauthorizedError, register_exception_handlers
from agent_runtime.api.routes import build_api_router, build_health_router
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
    format_sse_error,
    format_sse_event,
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
    "RuntimeMode",
    "ServerConfig",
    "StartRunRequest",
    "UnauthorizedError",
    "WorkflowInfo",
    "build_api_router",
    "build_health_router",
    "build_sse_response",
    "format_sse_error",
    "format_sse_event",
    "register_exception_handlers",
    "resolve_after_seq",
    "resolve_default_pi_bin",
    "resolve_run_model",
]
