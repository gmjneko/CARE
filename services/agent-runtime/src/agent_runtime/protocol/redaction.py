"""Credential redaction and diagnostic text sanitization helpers."""

from __future__ import annotations

import re

_MAX_FAILURE_MESSAGE_CHARS = 4096

_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization header / bearer / basic tokens
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    # URL userinfo credentials (https://user:pass@host)
    (re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@"), r"\1[REDACTED]:[REDACTED]@"),
    # Common API key prefixes (OpenAI / Anthropic / DeepSeek / generic sk-...)
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}\b"), "[REDACTED_KEY]"),
    # JSON-style secret fields
    (
        re.compile(
            r'(?i)("(?:api[_-]?key|token|access[_-]?token|secret|password|authorization)"\s*:\s*")([^"]+)(")'
        ),
        r"\1[REDACTED]\3",
    ),
)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact_sensitive_text(text: str, *, max_chars: int = _MAX_FAILURE_MESSAGE_CHARS) -> str:
    """Strip credentials, control characters, and excessive length from diagnostic text."""
    sanitized = text
    for pattern, replacement in _REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    sanitized = _CONTROL_CHARS_RE.sub("", sanitized).strip()
    if len(sanitized) > max_chars:
        return sanitized[: max_chars - 3] + "..."
    return sanitized


__all__ = ["redact_sensitive_text"]
