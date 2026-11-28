"""SSE cursor parsing, wire framing, and keepalive streaming over RunSubscription."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from agent_runtime.adapters import AdapterError
from agent_runtime.errors import InvalidInputError
from agent_runtime.protocol import RuntimeErrorEvent, RuntimeEvent
from agent_runtime.runtime import RunSubscription

logger = logging.getLogger(__name__)


class EventStreamResponse(StreamingResponse):
    """StreamingResponse specialization declaring `text/event-stream` for OpenAPI generation."""

    media_type = "text/event-stream"


def resolve_after_seq(after_seq: int | None, last_event_id: str | None) -> int:
    """Resolve SSE replay cursor from explicit `after_seq` query param or `Last-Event-ID`."""
    if after_seq is not None:
        if after_seq < 0:
            raise InvalidInputError("after_seq must be non-negative")
        return after_seq
    if last_event_id is not None and last_event_id.strip():
        raw = last_event_id.strip()
        try:
            parsed = int(raw)
        except ValueError as exc:
            raise InvalidInputError(
                f"Invalid Last-Event-ID header '{raw}': expected non-negative int"
            ) from exc
        if parsed < 0:
            raise InvalidInputError("Last-Event-ID must be non-negative")
        return parsed
    return 0


def format_sse_event(event: RuntimeEvent) -> str:
    """Serialize any canonical RuntimeEvent (including runtime.error) as a standard SSE frame."""
    return f"id: {event.seq}\nevent: {event.payload.type}\ndata: {event.model_dump_json()}\n\n"


def build_sse_response(
    subscription: RunSubscription,
    *,
    keepalive_s: float,
) -> StreamingResponse:
    """Create a cancellation-safe SSE StreamingResponse over a pinned RunSubscription."""

    async def _event_generator() -> AsyncIterator[str]:
        next_task: asyncio.Task[RuntimeEvent] | None = None
        try:
            while True:
                if next_task is None:
                    next_task = asyncio.create_task(anext(subscription))
                done, _ = await asyncio.wait({next_task}, timeout=keepalive_s)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                task = next_task
                next_task = None
                try:
                    event = task.result()
                except StopAsyncIteration:
                    return
                except AdapterError as exc:
                    error_event = RuntimeEvent(
                        seq=subscription.last_seq + 1,
                        run_id=subscription.run_id,
                        thread_id=subscription.thread_id,
                        workflow_id=subscription.workflow_id,
                        payload=RuntimeErrorEvent.from_raw(code="adapter_error", message=str(exc)),
                    )
                    yield format_sse_event(error_event)
                    return
                except Exception:
                    logger.exception(
                        "Unhandled error while streaming SSE for run_id '%s'",
                        subscription.run_id,
                    )
                    error_event = RuntimeEvent(
                        seq=subscription.last_seq + 1,
                        run_id=subscription.run_id,
                        thread_id=subscription.thread_id,
                        workflow_id=subscription.workflow_id,
                        payload=RuntimeErrorEvent(
                            code="internal_error",
                            message="Internal server error",
                        ),
                    )
                    yield format_sse_event(error_event)
                    return
                yield format_sse_event(event)
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                with contextlib.suppress(BaseException):
                    await next_task
            with contextlib.suppress(BaseException):
                await subscription.aclose()

    return EventStreamResponse(
        _event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def patch_sse_openapi(app: FastAPI) -> None:
    """Strip conflicting `type: string` next to `$ref` on the SSE endpoint OpenAPI schema.

    FastAPI merges a default `type: "string"` for non-JSON `text/*` response classes even when
    `responses[200]` supplies a Pydantic `model`.
    """
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


__all__ = [
    "EventStreamResponse",
    "build_sse_response",
    "format_sse_event",
    "patch_sse_openapi",
    "resolve_after_seq",
]
