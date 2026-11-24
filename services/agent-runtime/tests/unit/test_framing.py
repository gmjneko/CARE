"""Failure-mode-first tests for strict LF-only JSONL framing."""

from __future__ import annotations

import pytest

from agent_runtime.adapters.errors import AdapterProtocolError
from agent_runtime.adapters.pi.framing import JsonlFramer


def test_splits_multiple_objects_and_handles_split_utf8_across_chunks() -> None:
    framer = JsonlFramer()
    # Multi-byte Chinese characters and emoji split right in the middle of UTF-8 sequences
    payload = '{"type":"text","delta":"你好世界🚀"}\n{"type":"done"}\n'.encode()
    split_at = payload.index("世".encode()) + 1

    first = framer.feed(payload[:split_at])
    second = framer.feed(payload[split_at:])
    framer.finish()

    assert first == []
    assert second == [
        {"type": "text", "delta": "你好世界🚀"},
        {"type": "done"},
    ]


def test_strips_crlf_without_corrupting_escaped_cr() -> None:
    framer = JsonlFramer()
    records = framer.feed(b'{"a":"x\\ry"}\r\n{"b":2}\n')
    framer.finish()

    assert records == [{"a": "x\ry"}, {"b": 2}]


def test_does_not_split_on_unicode_line_or_paragraph_separators() -> None:
    framer = JsonlFramer()
    raw = ('{"text":"before\u2028middle\u2029after"}\n').encode("utf-8")

    records = framer.feed(raw)
    framer.finish()

    assert records == [{"text": "before\u2028middle\u2029after"}]


def test_exact_max_line_bytes_succeeds_and_oversized_fails_immediately() -> None:
    exact_line = b'{"a":"1234"}'
    framer_ok = JsonlFramer(max_line_bytes=len(exact_line))
    assert framer_ok.feed(exact_line + b"\n") == [{"a": "1234"}]

    framer_overflow = JsonlFramer(max_line_bytes=10)
    # Must fail before receiving '\n' so unbounded memory cannot accumulate
    with pytest.raises(AdapterProtocolError, match="exceeds maximum"):
        framer_overflow.feed(b'{"too":"long_without_newline"}')


def test_empty_line_fails_fast() -> None:
    framer = JsonlFramer()
    with pytest.raises(AdapterProtocolError, match="Empty line"):
        framer.feed(b"\n")


def test_invalid_utf8_fails_fast() -> None:
    framer = JsonlFramer()
    with pytest.raises(AdapterProtocolError, match="Invalid UTF-8"):
        framer.feed(b"\xff\xfe\n")


def test_non_json_stdout_line_fails_fast() -> None:
    framer = JsonlFramer()
    with pytest.raises(AdapterProtocolError, match="Malformed JSON"):
        framer.feed(b"console.log noise from extension\n")


@pytest.mark.parametrize("raw", [b"[1, 2, 3]\n", b'"scalar"\n', b"42\n", b"null\n"])
def test_non_object_json_frame_fails_fast(raw: bytes) -> None:
    framer = JsonlFramer()
    with pytest.raises(AdapterProtocolError, match="Expected JSON object"):
        framer.feed(raw)


def test_incomplete_trailing_bytes_at_eof_fails_fast() -> None:
    framer = JsonlFramer()
    assert framer.feed(b'{"type":"partial"') == []
    with pytest.raises(AdapterProtocolError, match="Incomplete JSONL frame"):
        framer.finish()
