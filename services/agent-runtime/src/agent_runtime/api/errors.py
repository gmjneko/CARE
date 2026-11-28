"""HTTP error envelope formatting and FastAPI exception handler registration."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_runtime.adapters import AdapterCapabilityError, AdapterError
from agent_runtime.api.schemas import ErrorDetail, ErrorResponse
from agent_runtime.errors import InvalidInputError, NotFoundError, StateConflictError
from agent_runtime.protocol import redact_sensitive_text

logger = logging.getLogger(__name__)

_HTTP_ERROR_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
    422: "validation_error",
}


class UnauthorizedError(Exception):
    """Raised when Bearer authentication fails on a protected route."""


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=redact_sensitive_text(message, max_chars=512),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def format_validation_error(exc: RequestValidationError | ValidationError) -> str:
    """Format Pydantic validation errors without raw input values so secrets are never echoed."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(segment) for segment in err.get("loc", ()))
        msg = str(err.get("msg", "Invalid input"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Request validation failed"


def register_exception_handlers(app: FastAPI) -> None:
    """Register redacted HTTP exception handlers mapping explicit domain errors to status codes."""

    @app.exception_handler(UnauthorizedError)
    async def _handle_unauthorized(_request: Request, exc: UnauthorizedError) -> JSONResponse:
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(NotFoundError)
    async def _handle_not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "not_found", str(exc))

    @app.exception_handler(StateConflictError)
    async def _handle_conflict(_request: Request, exc: StateConflictError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "conflict", str(exc))

    @app.exception_handler(AdapterCapabilityError)
    async def _handle_capability_error(
        _request: Request, exc: AdapterCapabilityError
    ) -> JSONResponse:
        return _error_response(status.HTTP_400_BAD_REQUEST, "unsupported_capability", str(exc))

    @app.exception_handler(InvalidInputError)
    async def _handle_bad_request(_request: Request, exc: InvalidInputError) -> JSONResponse:
        return _error_response(status.HTTP_400_BAD_REQUEST, "bad_request", str(exc))

    @app.exception_handler(AdapterError)
    async def _handle_adapter_error(_request: Request, exc: AdapterError) -> JSONResponse:
        return _error_response(status.HTTP_502_BAD_GATEWAY, "adapter_error", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(422, "validation_error", format_validation_error(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _HTTP_ERROR_CODES.get(exc.status_code, f"http_{exc.status_code}")
        message = str(exc.detail) if exc.detail else f"HTTP {exc.status_code}"
        headers = dict(exc.headers) if exc.headers else None
        return _error_response(exc.status_code, code, message, headers=headers)

    @app.exception_handler(Exception)
    async def _handle_internal_error(request: Request, _exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled error while processing %s %s",
            request.method,
            request.url.path,
        )
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "Internal server error",
        )


__all__ = [
    "UnauthorizedError",
    "format_validation_error",
    "register_exception_handlers",
]
