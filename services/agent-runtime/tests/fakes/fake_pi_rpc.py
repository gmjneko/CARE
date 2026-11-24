"""Out-of-process Fake Pi RPC subprocess for production-path E2E testing.

Speaks the real `pi --mode rpc` JSONL wire protocol over stdin/stdout so E2E tests
exercise the real subprocess transport, LF framing, request correlation, normalizer,
and LangGraph orchestrator without mocking internal modules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


def _emit(record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()


def _read_text_or_file(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--scenario", default="standard")
    parser.add_argument("--mode", default="rpc")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--session", default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--thinking", default=None)
    parser.add_argument("--system-prompt", default=None)
    parser.add_argument("--append-system-prompt", default=None)
    parser.add_argument("--tools", default=None)
    parser.add_argument("--no-tools", action="store_true")
    parser.add_argument("--extension", action="append", default=[])
    args, _ = parser.parse_known_args()
    return args


def _read_record() -> dict[str, Any] | None:
    raw = sys.stdin.buffer.readline()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _state_payload(
    *,
    is_streaming: bool = False,
    is_compacting: bool = False,
    pending: int = 0,
    session_file: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "sessionId": "fake-session-001",
        "isStreaming": is_streaming,
        "isCompacting": is_compacting,
        "pendingMessageCount": pending,
        "thinkingLevel": "low",
    }
    if session_file:
        data["sessionFile"] = session_file
    return data


def _handle_standard(
    req_id: str | None,
    prompt: str,
    *,
    tools: str | None,
    append_prompt: str,
) -> None:
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    _emit({"type": "agent_start"})
    _emit({"type": "message_start", "message": {"role": "assistant"}})
    _emit(
        {
            "type": "message_update",
            "usage": {"input": 50, "cacheRead": 10, "output": 5, "totalTokens": 65},
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "delta": "planning",
                "contentIndex": 0,
            },
        }
    )

    is_planner = "Planner agent" in append_prompt or tools == "read,grep,find,ls"
    role_tag = "PLANNER" if is_planner else "EXECUTOR"

    _emit(
        {
            "type": "tool_execution_start",
            "toolCallId": f"tc-{role_tag.lower()}-1",
            "toolName": "read" if is_planner else "bash",
            "args": {"path": "README.md"} if is_planner else {"command": "pytest"},
        }
    )
    # Emit cumulative updates so E2E verifies real delta slicing over stdio.
    _emit(
        {
            "type": "tool_execution_update",
            "toolCallId": f"tc-{role_tag.lower()}-1",
            "toolName": "read" if is_planner else "bash",
            "partialResult": {"content": [{"type": "text", "text": "part-1\n"}]},
        }
    )
    _emit(
        {
            "type": "tool_execution_update",
            "toolCallId": f"tc-{role_tag.lower()}-1",
            "toolName": "read" if is_planner else "bash",
            "partialResult": {"content": [{"type": "text", "text": "part-1\npart-2\n"}]},
        }
    )
    _emit(
        {
            "type": "tool_execution_end",
            "toolCallId": f"tc-{role_tag.lower()}-1",
            "toolName": "read" if is_planner else "bash",
            "result": {"content": [{"type": "text", "text": "part-1\npart-2\n"}]},
            "isError": False,
            "durationMs": 12,
        }
    )

    if is_planner:
        final_text = (
            f"[PLAN tools={tools}] Step 1: update module for prompt: {prompt.splitlines()[-1]}"
        )
    else:
        has_plan = "[PLAN tools=" in prompt
        final_text = f"[EXEC tools={tools or 'default'} received_plan={has_plan}] Done."

    mid = len(final_text) // 2
    _emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": final_text[:mid],
                "contentIndex": 1,
            },
        }
    )
    _emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": final_text[mid:],
                "contentIndex": 1,
            },
        }
    )
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "fake",
                "model": "fake-v1",
                "stopReason": "stop",
                "usage": {"inputTokens": 60, "outputTokens": 20, "totalTokens": 80},
                "content": [
                    {"type": "thinking", "thinking": "planning"},
                    {"type": "text", "text": final_text},
                ],
            },
        }
    )
    _emit({"type": "agent_end"})
    _emit({"type": "agent_settled", "aborted": False})


def _handle_ui_confirm(req_id: str | None) -> None:
    # Emit blocking UI dialog BEFORE acking `prompt` to prove non-blocking prompt send
    # does not deadlock extension dialogs (a key Pi RPC constraint).
    _emit(
        {
            "type": "extension_ui_request",
            "id": "ui-confirm-1",
            "method": "confirm",
            "title": "Allow dangerous action?",
            "message": "Run deployment script",
        }
    )
    reply = _read_record()
    assert reply is not None and reply.get("type") == "extension_ui_response"
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    outcome = (
        "cancelled"
        if reply.get("cancelled")
        else ("approved" if reply.get("confirmed") else "denied")
    )
    text = f"UI_CONFIRM_OUTCOME={outcome}"
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": text}],
            },
        }
    )
    _emit({"type": "agent_settled", "aborted": False})


def _handle_abortable(req_id: str | None) -> None:
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    _emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": "working...",
                "contentIndex": 0,
            },
        }
    )
    seen_commands: list[str] = []
    while True:
        cmd = _read_record()
        if cmd is None:
            return
        cmd_type = cmd.get("type")
        if cmd_type == "clear_queue":
            seen_commands.append("clear_queue")
            _emit(
                {"type": "response", "id": cmd.get("id"), "command": "clear_queue", "success": True}
            )
        elif cmd_type == "abort":
            seen_commands.append("abort")
            _emit({"type": "response", "id": cmd.get("id"), "command": "abort", "success": True})
            _emit(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "aborted",
                        "content": [
                            {"type": "text", "text": f"ABORT_ORDER={','.join(seen_commands)}"}
                        ],
                    },
                }
            )
            _emit({"type": "agent_settled", "aborted": True})
            return
        elif cmd_type == "get_state":
            _emit(
                {
                    "type": "response",
                    "id": cmd.get("id"),
                    "command": "get_state",
                    "success": True,
                    "data": _state_payload(is_streaming=True),
                }
            )


def _handle_steerable(req_id: str | None) -> None:
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    _emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "draft...", "contentIndex": 0},
        }
    )
    steered_text = ""
    while True:
        cmd = _read_record()
        if cmd is None:
            return
        if cmd.get("type") == "prompt" and cmd.get("streamingBehavior") == "steer":
            steered_text = str(cmd.get("message", ""))
            _emit(
                {
                    "type": "response",
                    "id": cmd.get("id"),
                    "command": "prompt",
                    "success": True,
                    "data": {"disposition": "queued"},
                }
            )
            break
    final_text = f"STEERED={steered_text}"
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": final_text}],
            },
        }
    )
    _emit({"type": "agent_settled", "aborted": False})


def _handle_provider_fail_planner(req_id: str | None, tools: str | None) -> None:
    if tools != "read,grep,find,ls":
        # Executor must never execute after planner fails.
        sys.stderr.write("ERROR: downstream executor node should not have been invoked\n")
        sys.stderr.flush()
        sys.exit(99)
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "429 rate limit exceeded with Bearer secret_xyz_1234567890",
                "content": [],
            },
        }
    )
    _emit({"type": "agent_settled", "aborted": False})


def _handle_compaction_race(req_id: str | None, prompt: str, state: dict[str, bool]) -> None:
    if not state["compacted_once"]:
        state["compacted_once"] = True
        _emit(
            {
                "type": "response",
                "id": req_id,
                "command": "prompt",
                "success": False,
                "error": "Cannot submit a prompt while compaction is in progress",
            }
        )
        _emit({"type": "compaction_start", "reason": "threshold"})
        _emit({"type": "compaction_end", "summary": "compacted 10 messages"})
        return

    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": f"AFTER_COMPACTION:{prompt}"}],
            },
        }
    )
    _emit({"type": "agent_settled", "aborted": False})


def _handle_inspect_endpoint(req_id: str | None, args: argparse.Namespace) -> None:
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    env_key = os.environ.get("CARE_LLM_API_KEY")
    models_raw: str | None = None
    models_json: dict[str, Any] | None = None
    if agent_dir:
        models_file = Path(agent_dir) / "models.json"
        if models_file.is_file():
            models_raw = models_file.read_text(encoding="utf-8")
            models_json = json.loads(models_raw)

    report = {
        "agent_dir": agent_dir,
        "provider": args.provider,
        "model": args.model,
        "thinking": args.thinking,
        "models_json": models_json,
        "env_api_key_set": env_key is not None,
        "env_api_key_sha256": (
            hashlib.sha256(env_key.encode("utf-8")).hexdigest()[:16] if env_key else None
        ),
        "api_key_in_argv": bool(env_key and any(env_key in part for part in sys.argv)),
        "api_key_in_models_file": bool(env_key and models_raw and env_key in models_raw),
    }
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": json.dumps(report, sort_keys=True)}],
            },
        }
    )
    _emit({"type": "agent_settled", "aborted": False})


def main() -> int:
    args = _parse_args()
    append_prompt = _read_text_or_file(args.append_system_prompt)
    session_file = args.session if args.session else (None if args.no_session else "fake.jsonl")
    compaction_state = {"compacted_once": False}

    # Prevent orphaned test helper if parent ever dies unexpectedly.
    timer = threading.Timer(30.0, lambda: sys.exit(124))
    timer.daemon = True
    timer.start()

    while True:
        record = _read_record()
        if record is None:
            return 0

        req_type = record.get("type")
        req_id = record.get("id")

        if req_type == "get_state":
            _emit(
                {
                    "type": "response",
                    "id": req_id,
                    "command": "get_state",
                    "success": True,
                    "data": _state_payload(session_file=session_file),
                }
            )
            continue

        if req_type == "prompt":
            prompt = str(record.get("message", ""))
            scenario = args.scenario
            if scenario == "standard":
                _handle_standard(req_id, prompt, tools=args.tools, append_prompt=append_prompt)
            elif scenario == "ui_confirm":
                _handle_ui_confirm(req_id)
            elif scenario == "abortable":
                _handle_abortable(req_id)
            elif scenario == "steerable":
                _handle_steerable(req_id)
            elif scenario == "provider_fail_planner":
                _handle_provider_fail_planner(req_id, args.tools)
            elif scenario == "compaction_race":
                _handle_compaction_race(req_id, prompt, compaction_state)
            elif scenario == "inspect_endpoint":
                _handle_inspect_endpoint(req_id, args)
            elif scenario == "malformed_stdout":
                sys.stdout.buffer.write(b"console.log accidental stdout noise\n")
                sys.stdout.buffer.flush()
                return 0
            elif scenario == "crash_mid_turn":
                _emit(
                    {
                        "type": "response",
                        "id": req_id,
                        "command": "prompt",
                        "success": True,
                        "data": {"disposition": "started"},
                    }
                )
                _emit(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "delta": "partial ",
                            "contentIndex": 0,
                        },
                    }
                )
                sys.stderr.write("fatal crash sk-proj-1234567890abcdef123456\n")
                sys.stderr.flush()
                return 137
            else:
                raise ValueError(f"Unknown fake scenario: {scenario}")


if __name__ == "__main__":
    raise SystemExit(main())
