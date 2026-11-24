"""Failure-mode-first tests for Pi RPC event normalization."""

from __future__ import annotations

import pytest

from agent_runtime.adapters.errors import AdapterProtocolError
from agent_runtime.adapters.pi.normalizer import PiEventNormalizer
from agent_runtime.protocol.events import (
    CompactionEvent,
    InteractionRequestEvent,
    MessageCompletedEvent,
    RetryEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TokenUsage,
    ToolCompletedEvent,
    ToolOutputDeltaEvent,
    ToolStartedEvent,
    UsageUpdatedEvent,
)


def test_normalizes_text_thinking_usage_and_final_message() -> None:
    norm = PiEventNormalizer()

    events1 = norm.consume(
        {
            "type": "message_update",
            "usage": {"input": 100, "cacheRead": 20, "output": 15, "totalTokens": 135},
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm", "contentIndex": 0},
        }
    )
    assert events1 == [
        UsageUpdatedEvent(
            usage=TokenUsage(
                input_tokens=120,
                output_tokens=15,
                cached_input_tokens=20,
                total_tokens=135,
            )
        ),
        ThinkingDeltaEvent(delta="hmm", content_index=0),
    ]

    events2 = norm.consume(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "hello ", "contentIndex": 1},
        }
    )
    assert events2 == [TextDeltaEvent(delta="hello ", content_index=1)]

    events3 = norm.consume(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "deepseek",
                "model": "deepseek-flash",
                "stopReason": "stop",
                "usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "hello world"},
                ],
            },
        }
    )
    assert events3 == [
        MessageCompletedEvent(
            role="assistant",
            text="hello world",
            thinking="hmm",
            model="deepseek/deepseek-flash",
            usage=TokenUsage(input_tokens=120, output_tokens=30, total_tokens=150),
        )
    ]
    assert norm.final_text == "hello world"
    assert norm.latest_usage == TokenUsage(input_tokens=120, output_tokens=30, total_tokens=150)


def test_user_message_end_does_not_overwrite_assistant_final_text() -> None:
    norm = PiEventNormalizer()
    norm.consume(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "answer"}],
            },
        }
    )
    norm.consume(
        {
            "type": "message_end",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "follow up prompt"}],
            },
        }
    )
    assert norm.final_text == "answer"


def test_tool_execution_update_diffs_cumulative_tail_and_handles_window_shift() -> None:
    norm = PiEventNormalizer()
    assert norm.consume(
        {
            "type": "tool_execution_start",
            "toolCallId": "tc-1",
            "toolName": "bash",
            "args": {"command": "pytest"},
        }
    ) == [
        ToolStartedEvent(
            tool_call_id="tc-1",
            tool_name="bash",
            arguments={"command": "pytest"},
        )
    ]

    # First chunk
    assert norm.consume(
        {
            "type": "tool_execution_update",
            "toolCallId": "tc-1",
            "toolName": "bash",
            "partialResult": {"content": [{"type": "text", "text": "line1\n"}]},
        }
    ) == [ToolOutputDeltaEvent(tool_call_id="tc-1", tool_name="bash", chunk="line1\n")]

    # Cumulative prefix growth -> only new tail emitted
    assert norm.consume(
        {
            "type": "tool_execution_update",
            "toolCallId": "tc-1",
            "toolName": "bash",
            "partialResult": {"content": [{"type": "text", "text": "line1\nline2\n"}]},
        }
    ) == [ToolOutputDeltaEvent(tool_call_id="tc-1", tool_name="bash", chunk="line2\n")]

    # Exact duplicate -> suppressed
    assert (
        norm.consume(
            {
                "type": "tool_execution_update",
                "toolCallId": "tc-1",
                "toolName": "bash",
                "partialResult": {"content": [{"type": "text", "text": "line1\nline2\n"}]},
            }
        )
        == []
    )

    # Sliding window tail shift (head dropped by Pi) -> suffix-prefix overlap emits only new line
    assert norm.consume(
        {
            "type": "tool_execution_update",
            "toolCallId": "tc-1",
            "toolName": "bash",
            "partialResult": {"content": [{"type": "text", "text": "line2\nline3\n"}]},
        }
    ) == [ToolOutputDeltaEvent(tool_call_id="tc-1", tool_name="bash", chunk="line3\n")]

    assert norm.consume(
        {
            "type": "tool_execution_end",
            "toolCallId": "tc-1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "line1\nline2\nline3\n"}]},
            "isError": False,
            "durationMs": 42,
        }
    ) == [
        ToolCompletedEvent(
            tool_call_id="tc-1",
            tool_name="bash",
            output="line1\nline2\nline3\n",
            is_error=False,
            duration_ms=42,
        )
    ]


def test_error_message_redacts_secrets_and_classifies_rate_limit() -> None:
    norm = PiEventNormalizer()
    norm.consume(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": (
                    "429 Rate limit exceeded calling https://alice:s3cr3t@api.example.com "
                    "with Bearer eyJhbGciOiJIUzI1NiJ9 and key sk-ant-1234567890abcdef123456"
                ),
                "content": [],
            },
        }
    )
    failure = norm.failure
    assert failure is not None
    assert failure.failure_class == "usage_limit"
    assert failure.retryable is True
    assert "s3cr3t" not in failure.message
    assert "eyJhbGciOiJIUzI1NiJ9" not in failure.message
    assert "sk-ant-1234567890abcdef123456" not in failure.message


def test_compaction_and_retry_recovery_clears_transient_failure() -> None:
    norm = PiEventNormalizer()

    assert norm.consume({"type": "compaction_start", "reason": "threshold"}) == [
        CompactionEvent(phase="started", reason="threshold")
    ]
    assert norm.is_compacting is True

    assert norm.consume({"type": "compaction_end", "summary": "trimmed"}) == [
        CompactionEvent(phase="completed", summary="trimmed")
    ]
    assert norm.is_compacting is False

    # Transient provider error followed by successful auto-retry clears failure
    norm.consume(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "502 bad gateway",
            },
        }
    )
    assert norm.failure is not None

    assert norm.consume(
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "errorMessage": "502"}
    ) == [RetryEvent(phase="started", attempt=1, max_attempts=3, error="502")]

    assert norm.consume({"type": "auto_retry_end", "attempt": 1, "success": True}) == [
        RetryEvent(phase="completed", attempt=1, succeeded=True)
    ]
    assert norm.failure is None


def test_extension_ui_request_maps_to_backend_agnostic_interactions() -> None:
    norm = PiEventNormalizer()

    assert norm.consume(
        {
            "type": "extension_ui_request",
            "id": "ui-1",
            "method": "confirm",
            "title": "Allow bash?",
            "message": "rm -rf /tmp/x",
        }
    ) == [
        InteractionRequestEvent(
            request_id="ui-1",
            kind="approval",
            blocking=True,
            title="Allow bash?",
            message="rm -rf /tmp/x",
        )
    ]

    assert norm.consume(
        {
            "type": "extension_ui_request",
            "id": "ui-select",
            "method": "select",
            "title": "Pick database",
            "options": ["sqlite", "postgres"],
        }
    ) == [
        InteractionRequestEvent(
            request_id="ui-select",
            kind="user_input",
            blocking=True,
            title="Pick database",
            input_mode="select",
            options=("sqlite", "postgres"),
        )
    ]

    assert norm.consume(
        {
            "type": "extension_ui_request",
            "id": "ui-2",
            "method": "notify",
            "message": "Indexing done",
        }
    ) == [
        InteractionRequestEvent(
            request_id="ui-2",
            kind="notify",
            blocking=False,
            message="Indexing done",
        )
    ]

    # Pure TUI chrome fire-and-forget methods do not emit blocking interaction requests
    assert (
        norm.consume(
            {"type": "extension_ui_request", "id": "ui-3", "method": "setStatus", "text": "ok"}
        )
        == []
    )


def test_malformed_frames_fail_fast() -> None:
    norm = PiEventNormalizer()

    with pytest.raises(AdapterProtocolError, match="missing string 'type'"):
        norm.consume({"foo": "bar"})

    with pytest.raises(AdapterProtocolError, match="extension_ui_request"):
        norm.consume({"type": "extension_ui_request", "method": "confirm"})
