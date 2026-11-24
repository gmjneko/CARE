"""Strict LF-only JSONL framing for Pi RPC stdout streams."""

from __future__ import annotations

import json
from typing import Any

from agent_runtime.adapters.errors import AdapterProtocolError
from agent_runtime.protocol.events import redact_sensitive_text

DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024  # 16 MiB


class JsonlFramer:
    """Incrementally decodes strictly LF-delimited UTF-8 JSON object frames.

    Splits only on ASCII LF (0x0A, stripping an optional preceding CR 0x0D) so
    Unicode line/paragraph separators (U+2028/U+2029) embedded in JSON strings
    never split a frame. Any protocol violation raises AdapterProtocolError.
    """

    def __init__(self, *, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> None:
        if max_line_bytes <= 0:
            raise ValueError("max_line_bytes must be positive")
        self._max_line_bytes = max_line_bytes
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if not chunk:
            return []

        records: list[dict[str, Any]] = []
        start = 0
        chunk_len = len(chunk)

        while start < chunk_len:
            nl = chunk.find(b"\n", start)
            if nl == -1:
                tail = chunk[start:]
                if len(self._buffer) + len(tail) > self._max_line_bytes:
                    self._buffer.clear()
                    raise AdapterProtocolError(
                        f"JSONL frame exceeds maximum allowed size ({self._max_line_bytes} bytes)"
                    )
                self._buffer.extend(tail)
                break

            segment = chunk[start:nl]
            # Allow one extra byte before stripping optional trailing CR.
            if len(self._buffer) + len(segment) > self._max_line_bytes + 1:
                self._buffer.clear()
                raise AdapterProtocolError(
                    f"JSONL frame exceeds maximum allowed size ({self._max_line_bytes} bytes)"
                )

            if self._buffer:
                self._buffer.extend(segment)
                raw_line = bytes(self._buffer)
                self._buffer.clear()
            else:
                raw_line = segment

            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]

            if len(raw_line) > self._max_line_bytes:
                raise AdapterProtocolError(
                    f"JSONL frame exceeds maximum allowed size ({self._max_line_bytes} bytes)"
                )

            records.append(self._decode_line(raw_line))
            start = nl + 1

        return records

    def finish(self) -> None:
        """Validate that no partial trailing frame remains at EOF."""
        if self._buffer:
            snippet = redact_sensitive_text(
                self._buffer[:200].decode("utf-8", errors="replace"), max_chars=200
            )
            self._buffer.clear()
            raise AdapterProtocolError(f"Incomplete JSONL frame at EOF: {snippet}")

    def _decode_line(self, raw_line: bytes) -> dict[str, Any]:
        if not raw_line:
            raise AdapterProtocolError("Empty line received on Pi JSONL stream")

        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdapterProtocolError("Invalid UTF-8 bytes in Pi JSONL frame") from exc

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = redact_sensitive_text(text, max_chars=200)
            raise AdapterProtocolError(f"Malformed JSON on Pi stdout: {snippet}") from exc

        if not isinstance(parsed, dict):
            raise AdapterProtocolError(
                f"Expected JSON object on Pi stdout, got {type(parsed).__name__}"
            )
        return parsed
