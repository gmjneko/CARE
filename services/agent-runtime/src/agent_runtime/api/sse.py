"""SSE cursor parsing, wire framing, and keepalive streaming over RunManager."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse

from agent_runtime.adapters import AdapterError
from agent_runtime.protocol import RuntimeEvent, redact_sensitive_text
from agent_runtime.runtime import RunManager


def resolve_after_seq(after_seq: int | None, last_event_id: str | None) -> int:
    """Resolve SSE replay cursor from explicit `after_seq` query param or `Last-Event-ID`."""
    if after_seq is not None:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        return after_seq
    if last_event_id is not None and last_event_id.strip():
        raw = last_event_id.strip()
        try:
            parsed = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"Invalid Last-Event-ID header '{raw}': expected non-negative int"
            ) from exc
        if parsed < 0:
            raise ValueError("Last-Event-ID must be non-negative")
        return parsed
    return 0


def format_sse_event(event: RuntimeEvent) -> str:
    """Serialize a RuntimeEvent as a standard SSE frame."""
    return f"id: {event.seq}\nevent: {event.payload.type}\ndata: {event.model_dump_json()}\n\n"


def format_sse_error(code: str, message: str) -> str:
    """Serialize a terminal redacted SSE error frame after response headers are sent."""
    body = json.dumps(
        {"code": code, "message": redact_sensitive_text(message, max_chars=512)},
        ensure_ascii=False,
    )
    return f"event: runtime.error\ndata: {body}\n\n"


def build_sse_response(
    manager: RunManager,
    run_id: str,
    *,
    start_seq: int,
    keepalive_s: float,
) -> StreamingResponse:
    """Create a cancellation-safe SSE StreamingResponse with periodic keepalive comments."""

    async def _event_generator() -> AsyncIterator[str]:
        stream = manager.subscribe(run_id, after_seq=start_seq)
        next_task: asyncio.Task[RuntimeEvent] | None = None
        try:
            while True:
                if next_task is None:
                    next_task = asyncio.create_task(anext(stream))
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
                    yield format_sse_error("adapter_error", str(exc))
                    return
                except Exception as exc:
                    yield format_sse_error("internal_error", str(exc))
                    return
                yield format_sse_event(event)
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                with contextlib.suppress(BaseException):
                    await next_task
            if hasattr(stream, "aclose"):
                with contextlib.suppress(BaseException):
                    await stream.aclose()

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "build_sse_response",
    "format_sse_error",
    "format_sse_event",
    "resolve_after_seq",
]
