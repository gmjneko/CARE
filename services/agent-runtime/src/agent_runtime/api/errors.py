"""HTTP error envelope formatting and FastAPI exception handler registration."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_runtime.adapters import AdapterCapabilityError, AdapterError
from agent_runtime.api.schemas import ErrorDetail, ErrorResponse
from agent_runtime.orchestrator import StateConflictError
from agent_runtime.protocol import redact_sensitive_text


class UnauthorizedError(Exception):
    """Raised when Bearer authentication fails on a protected route."""


def _key_error_message(exc: KeyError) -> str:
    return str(exc.args[0]) if exc.args else str(exc)


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


def register_exception_handlers(app: FastAPI) -> None:
    """Register redacted HTTP exception handlers mapping domain errors to status codes."""

    @app.exception_handler(UnauthorizedError)
    async def _handle_unauthorized(_request: Request, exc: UnauthorizedError) -> JSONResponse:
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            "unauthorized",
            str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(KeyError)
    async def _handle_not_found(_request: Request, exc: KeyError) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "not_found", _key_error_message(exc))

    @app.exception_handler(StateConflictError)
    async def _handle_conflict(_request: Request, exc: StateConflictError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "conflict", str(exc))

    @app.exception_handler(AdapterCapabilityError)
    async def _handle_capability_error(
        _request: Request, exc: AdapterCapabilityError
    ) -> JSONResponse:
        return _error_response(status.HTTP_400_BAD_REQUEST, "unsupported_capability", str(exc))

    @app.exception_handler(ValueError)
    async def _handle_bad_request(_request: Request, exc: ValueError) -> JSONResponse:
        return _error_response(status.HTTP_400_BAD_REQUEST, "bad_request", str(exc))

    @app.exception_handler(AdapterError)
    async def _handle_adapter_error(_request: Request, exc: AdapterError) -> JSONResponse:
        return _error_response(status.HTTP_502_BAD_GATEWAY, "adapter_error", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Strip raw `input` values from Pydantic errors so secrets like `api_key` are never echoed.
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(segment) for segment in err.get("loc", ()))
            msg = str(err.get("msg", "Invalid input"))
            parts.append(f"{loc}: {msg}" if loc else msg)
        detail = "; ".join(parts) or "Request validation failed"
        return _error_response(422, "validation_error", detail)

    @app.exception_handler(Exception)
    async def _handle_internal_error(_request: Request, exc: Exception) -> JSONResponse:
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            str(exc) or "Internal server error",
        )


__all__ = [
    "UnauthorizedError",
    "register_exception_handlers",
]
