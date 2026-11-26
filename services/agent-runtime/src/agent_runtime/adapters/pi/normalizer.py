"""Pure state machine translating raw Pi RPC events into canonical AgentEvents."""

from __future__ import annotations

from typing import Any, Literal

from agent_runtime.adapters.errors import AdapterProtocolError
from agent_runtime.protocol.events import (
    AgentEvent,
    AgentFailure,
    CompactionEvent,
    FailureClass,
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
from agent_runtime.protocol.redaction import redact_sensitive_text

_USER_INPUT_UI_METHODS: frozenset[str] = frozenset({"select", "input", "editor"})
_IGNORED_UI_METHODS: frozenset[str] = frozenset(
    {"setStatus", "setWidget", "setTitle", "set_editor_text", "setEditorText"}
)


def _longest_suffix_prefix_overlap(previous: str, current: str) -> int:
    """Return length of the longest suffix of `previous` matching a prefix of `current`."""
    n = len(current)
    if n == 0 or not previous:
        return 0

    lps = [0] * n
    j = 0
    for i in range(1, n):
        while j > 0 and current[i] != current[j]:
            j = lps[j - 1]
        if current[i] == current[j]:
            j += 1
            lps[i] = j

    tail = previous[-n:] if len(previous) > n else previous
    j = 0
    for ch in tail:
        if j == n:
            j = lps[j - 1]
        while j > 0 and ch != current[j]:
            j = lps[j - 1]
        if ch == current[j]:
            j += 1
    return j


def _compute_output_delta(previous: str, current: str) -> str:
    if not current or current == previous:
        return ""
    if not previous:
        return current
    if current.startswith(previous):
        return current[len(previous) :]
    overlap = _longest_suffix_prefix_overlap(previous, current)
    return current[overlap:]


def _extract_text_from_result(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)
        if isinstance(payload.get("text"), str):
            return payload["text"]
    return str(payload)


def _parse_usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None

    cached = int(raw.get("cacheRead") or raw.get("cachedInputTokens") or 0)
    cache_write = int(raw.get("cacheWrite") or 0)

    if "inputTokens" in raw:
        input_tokens = int(raw.get("inputTokens") or 0)
    else:
        input_tokens = int(raw.get("input") or 0) + cached + cache_write

    output_tokens = int(raw.get("outputTokens") or raw.get("output") or 0)
    total_tokens = int(raw.get("totalTokens") or (input_tokens + output_tokens))

    if input_tokens == 0 and output_tokens == 0 and total_tokens == 0 and cached == 0:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        total_tokens=total_tokens,
    )


def _classify_provider_failure(message: str) -> tuple[FailureClass, bool]:
    lower = message.lower()
    if any(k in lower for k in ("429", "rate limit", "rate_limit", "quota", "too many requests")):
        return "usage_limit", True
    if any(k in lower for k in ("401", "403", "unauthorized", "forbidden", "invalid api key")):
        return "permission_error", False
    if any(k in lower for k in ("502", "503", "504", "overloaded", "timeout", "temporarily")):
        return "provider_error", True
    return "provider_error", False


class PiEventNormalizer:
    """Stateful per-turn translator from Pi JsonAgentSessionEvent records to AgentEvents."""

    def __init__(self) -> None:
        self._streamed_text: list[str] = []
        self._final_assistant_text: str | None = None
        self._tool_outputs: dict[str, str] = {}
        self.latest_usage: TokenUsage | None = None
        self.failure: AgentFailure | None = None
        self.is_compacting: bool = False

    @property
    def final_text(self) -> str:
        if self._final_assistant_text is not None:
            return self._final_assistant_text
        return "".join(self._streamed_text)

    def consume(self, frame: dict[str, Any]) -> list[AgentEvent]:
        event_type = frame.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise AdapterProtocolError("Pi event frame is missing string 'type'")

        if event_type == "message_update":
            return self._handle_message_update(frame)
        if event_type == "message_end":
            return self._handle_message_end(frame)
        if event_type == "tool_execution_start":
            return self._handle_tool_start(frame)
        if event_type == "tool_execution_update":
            return self._handle_tool_update(frame)
        if event_type == "tool_execution_end":
            return self._handle_tool_end(frame)
        if event_type == "compaction_start":
            self.is_compacting = True
            return [CompactionEvent(phase="started", reason=_opt_str(frame.get("reason")))]
        if event_type == "compaction_end":
            self.is_compacting = False
            return [CompactionEvent(phase="completed", summary=_opt_str(frame.get("summary")))]
        if event_type == "auto_retry_start":
            err = _opt_str(frame.get("errorMessage"))
            return [
                RetryEvent(
                    phase="started",
                    attempt=int(frame.get("attempt") or 1),
                    max_attempts=_opt_int(frame.get("maxAttempts")),
                    error=redact_sensitive_text(err) if err else None,
                )
            ]
        if event_type == "auto_retry_end":
            succeeded = bool(frame.get("success", False))
            err = _opt_str(frame.get("finalError") or frame.get("errorMessage"))
            if succeeded:
                self.failure = None
            elif err:
                failure_class, retryable = _classify_provider_failure(err)
                self.failure = AgentFailure.from_raw(
                    failure_class=failure_class,
                    message=err,
                    retryable=retryable,
                )
            return [
                RetryEvent(
                    phase="completed",
                    attempt=int(frame.get("attempt") or 1),
                    error=redact_sensitive_text(err) if err else None,
                    succeeded=succeeded,
                )
            ]
        if event_type == "extension_ui_request":
            return self._handle_ui_request(frame)
        if event_type == "extension_error":
            err = _opt_str(frame.get("error") or frame.get("message")) or "Extension error"
            self.failure = AgentFailure.from_raw(failure_class="provider_error", message=err)
            return []

        # Informational lifecycle events (agent_start, turn_start, turn_end, message_start,
        # agent_end, queue_update, etc.) carry no additional canonical payload here.
        return []

    def _handle_message_update(self, frame: dict[str, Any]) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        usage = _parse_usage(frame.get("usage"))
        if usage is not None:
            self.latest_usage = usage
            events.append(UsageUpdatedEvent(usage=usage))

        sub = frame.get("assistantMessageEvent")
        if not isinstance(sub, dict):
            return events

        sub_type = sub.get("type")
        content_index = int(sub.get("contentIndex") or 0)
        if sub_type == "text_delta":
            delta = sub.get("delta")
            if isinstance(delta, str) and delta:
                self._streamed_text.append(delta)
                events.append(TextDeltaEvent(delta=delta, content_index=content_index))
        elif sub_type == "thinking_delta":
            delta = sub.get("delta")
            if isinstance(delta, str) and delta:
                events.append(ThinkingDeltaEvent(delta=delta, content_index=content_index))
        return events

    def _handle_message_end(self, frame: dict[str, Any]) -> list[AgentEvent]:
        message = frame.get("message")
        if not isinstance(message, dict):
            return []

        raw_role = message.get("role")
        if raw_role not in ("user", "assistant"):
            return []
        role: Literal["user", "assistant"] = raw_role

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        content = message.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block_type == "thinking" and isinstance(block.get("thinking"), str):
                    thinking_parts.append(block["thinking"])

        text = "".join(text_parts)
        thinking = "".join(thinking_parts) or None

        provider = _opt_str(message.get("provider"))
        model_id = _opt_str(message.get("model"))
        model = f"{provider}/{model_id}" if provider and model_id else model_id

        usage = _parse_usage(message.get("usage"))
        if usage is not None:
            self.latest_usage = usage

        if role == "assistant":
            if text:
                self._final_assistant_text = text
            if message.get("stopReason") == "error":
                raw_err = _opt_str(message.get("errorMessage")) or "Model response failed"
                failure_class, retryable = _classify_provider_failure(raw_err)
                self.failure = AgentFailure.from_raw(
                    failure_class=failure_class,
                    message=raw_err,
                    retryable=retryable,
                )

        return [
            MessageCompletedEvent(
                role=role,
                text=text,
                thinking=thinking,
                model=model,
                usage=usage,
            )
        ]

    def _handle_tool_start(self, frame: dict[str, Any]) -> list[AgentEvent]:
        tool_call_id = _require_str(frame, "toolCallId", "tool_execution_start")
        tool_name = _require_str(frame, "toolName", "tool_execution_start")
        args = frame.get("args")
        arguments = args if isinstance(args, dict) else {}
        self._tool_outputs[tool_call_id] = ""
        return [
            ToolStartedEvent(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                parent_tool_call_id=_opt_str(frame.get("parentToolCallId")),
            )
        ]

    def _handle_tool_update(self, frame: dict[str, Any]) -> list[AgentEvent]:
        tool_call_id = _require_str(frame, "toolCallId", "tool_execution_update")
        tool_name = _require_str(frame, "toolName", "tool_execution_update")
        current = _extract_text_from_result(frame.get("partialResult"))
        previous = self._tool_outputs.get(tool_call_id, "")
        delta = _compute_output_delta(previous, current)
        if current:
            self._tool_outputs[tool_call_id] = current
        if not delta:
            return []
        return [
            ToolOutputDeltaEvent(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                chunk=delta,
            )
        ]

    def _handle_tool_end(self, frame: dict[str, Any]) -> list[AgentEvent]:
        tool_call_id = _require_str(frame, "toolCallId", "tool_execution_end")
        tool_name = _require_str(frame, "toolName", "tool_execution_end")
        self._tool_outputs.pop(tool_call_id, None)
        return [
            ToolCompletedEvent(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                output=_extract_text_from_result(frame.get("result")),
                is_error=bool(frame.get("isError", False)),
                duration_ms=_opt_int(frame.get("durationMs")),
            )
        ]

    def _handle_ui_request(self, frame: dict[str, Any]) -> list[AgentEvent]:
        request_id = _require_str(frame, "id", "extension_ui_request")
        method = _require_str(frame, "method", "extension_ui_request")
        if method in _IGNORED_UI_METHODS:
            return []

        if method == "confirm":
            return [
                InteractionRequestEvent(
                    request_id=request_id,
                    kind="approval",
                    blocking=True,
                    title=_opt_str(frame.get("title")),
                    message=_opt_str(frame.get("message")),
                )
            ]

        if method in _USER_INPUT_UI_METHODS:
            raw_options = frame.get("options")
            options: tuple[str, ...] | None = None
            if isinstance(raw_options, list):
                options = tuple(str(opt) for opt in raw_options)
            input_mode: Literal["select", "text"] = "select" if method == "select" else "text"
            return [
                InteractionRequestEvent(
                    request_id=request_id,
                    kind="user_input",
                    blocking=True,
                    title=_opt_str(frame.get("title")),
                    message=_opt_str(frame.get("message")),
                    input_mode=input_mode,
                    options=options,
                )
            ]

        if method == "notify":
            return [
                InteractionRequestEvent(
                    request_id=request_id,
                    kind="notify",
                    blocking=False,
                    title=_opt_str(frame.get("title")),
                    message=_opt_str(frame.get("message")),
                )
            ]

        return []


def _require_str(frame: dict[str, Any], field: str, context: str) -> str:
    value = frame.get(field)
    if not isinstance(value, str) or not value:
        raise AdapterProtocolError(f"{context} is missing required string field '{field}'")
    return value


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None
