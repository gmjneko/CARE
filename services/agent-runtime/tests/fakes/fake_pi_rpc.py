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
from uuid import uuid4


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
    parser.add_argument("--fork", default=None)
    parser.add_argument("--session-dir", default=None)
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


def _is_planner_turn(
    prompt: str,
    append_prompt: str,
    tools: str | None,
    history: list[str] = (),
) -> bool:
    if "Currently in the **Execute** phase" in prompt or any(
        "Currently in the **Execute** phase" in item for item in history
    ):
        return False
    return (
        "Currently in the **Plan** phase" in prompt
        or any("Currently in the **Plan** phase" in item for item in history)
        or "Planner agent" in append_prompt
        or tools == "read,grep,find,ls"
    )


def _handle_standard(
    req_id: str | None,
    prompt: str,
    *,
    tools: str | None,
    append_prompt: str,
    session_file: str | None = None,
) -> None:
    history = _load_session_history(session_file)
    _append_session_history(session_file, prompt)
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

    is_planner = _is_planner_turn(prompt, append_prompt, tools, history)
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
            f"[PLAN tools={tools or 'default'}] Step 1: update module for prompt: "
            f"{prompt.splitlines()[-1]}"
        )
    else:
        has_plan = "[PLAN tools=" in prompt or any(
            "Currently in the **Plan** phase" in item for item in history
        )
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


def _handle_ui_abort_order(req_id: str | None) -> None:
    """Emit blocking confirm dialog, then record exact arrival order of cancel/clear_queue/abort."""
    _emit(
        {
            "type": "extension_ui_request",
            "id": "ui-abort-1",
            "method": "confirm",
            "title": "Confirm deploy",
            "message": "Proceed?",
        }
    )
    order: list[str] = []
    while True:
        cmd = _read_record()
        if cmd is None:
            return
        cmd_type = str(cmd.get("type") or "")
        order.append(cmd_type)
        if cmd_type == "extension_ui_response":
            _emit(
                {
                    "type": "response",
                    "id": req_id,
                    "command": "prompt",
                    "success": True,
                    "data": {"disposition": "started"},
                }
            )
        elif cmd_type == "clear_queue":
            _emit(
                {"type": "response", "id": cmd.get("id"), "command": "clear_queue", "success": True}
            )
        elif cmd_type == "abort":
            _emit({"type": "response", "id": cmd.get("id"), "command": "abort", "success": True})
            _emit(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "aborted",
                        "content": [{"type": "text", "text": f"ORDER={','.join(order)}"}],
                    },
                }
            )
            _emit({"type": "agent_settled", "aborted": True})
            return


def _handle_abortable(req_id: str | None, prompt: str, session_file: str | None) -> None:
    _append_session_history(session_file, prompt)
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


def _handle_steerable(req_id: str | None, *, reject_first_steer: bool = False) -> None:
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
    rejected = False
    while True:
        cmd = _read_record()
        if cmd is None:
            return
        if cmd.get("type") == "prompt" and cmd.get("streamingBehavior") == "steer":
            if reject_first_steer and not rejected:
                rejected = True
                _emit(
                    {
                        "type": "response",
                        "id": cmd.get("id"),
                        "command": "prompt",
                        "success": False,
                        "error": "Steer rejected while tool is running",
                    }
                )
                continue
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


def _handle_provider_fail_planner(
    req_id: str | None,
    prompt: str,
    append_prompt: str,
    tools: str | None,
) -> None:
    if not _is_planner_turn(prompt, append_prompt, tools):
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
        "tools": args.tools,
        "no_tools": args.no_tools,
        "system_prompt": _read_text_or_file(args.system_prompt) if args.system_prompt else None,
        "append_system_prompt": (
            _read_text_or_file(args.append_system_prompt) if args.append_system_prompt else None
        ),
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


def _load_session_history(session_file: str | None) -> list[str]:
    if not session_file:
        return []
    path = Path(session_file)
    if not path.is_file():
        return []
    history: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict) and isinstance(record.get("prompt"), str):
                history.append(record["prompt"])
    return history


def _append_session_history(session_file: str | None, prompt: str) -> None:
    if not session_file:
        return
    path = Path(session_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"prompt": prompt}, ensure_ascii=False) + "\n")


def _handle_stateful_chat(req_id: str | None, prompt: str, session_file: str | None) -> None:
    """多轮持久化会话场景：将每次 prompt 真实追加到 session JSONL 文件并回显完整轮次历史。"""
    history = _load_session_history(session_file)
    history.append(prompt)
    _append_session_history(session_file, prompt)

    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    reply = json.dumps(
        {
            "session_file": session_file,
            "turn_count": len(history),
            "history": history,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    _emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": reply, "contentIndex": 0},
        }
    )
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": reply}],
            },
        }
    )
    _emit({"type": "agent_settled", "aborted": False})


def _handle_ui_question(
    req_id: str | None,
    prompt: str,
    *,
    session_file: str | None,
    append_prompt: str,
    tools: str | None,
    crash_while_waiting: bool = False,
    question_id: str = "ui-q-1",
) -> bool:
    """模拟节点内 ask_question 阻塞提问；若已有历史则直接按恢复后回答继续完成。"""
    is_planner = _is_planner_turn(prompt, append_prompt, tools)
    if is_planner:
        _handle_standard(
            req_id, prompt, tools=tools, append_prompt=append_prompt, session_file=session_file
        )
        return False

    history = _load_session_history(session_file)
    prior_exec_turns = [item for item in history if "Currently in the **Plan** phase" not in item]
    _append_session_history(session_file, prompt)

    # 若已有本节点执行历史，说明是冷恢复后的续接轮次，直接消费恢复指令完成任务
    if prior_exec_turns:
        _emit(
            {
                "type": "response",
                "id": req_id,
                "command": "prompt",
                "success": True,
                "data": {"disposition": "started"},
            }
        )
        final_text = f"RESUMED_WITH_HISTORY({len(prior_exec_turns)}):{prompt}"
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
        return False

    _emit(
        {
            "type": "extension_ui_request",
            "id": question_id,
            "method": "select",
            "title": "Select cache backend",
            "message": "Which cache backend should be used?",
            "options": ["redis", "memory"],
        }
    )

    if crash_while_waiting:
        # 发出阻塞提问后立刻模拟子进程被外部强杀
        sys.stderr.write("killed while awaiting user answer\n")
        sys.stderr.flush()
        return True

    resp = _read_record()
    assert resp is not None and resp.get("type") == "extension_ui_response"
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    answer = "cancelled" if resp.get("cancelled") else str(resp.get("value") or "")
    final_text = f"QUESTION_ANSWER={answer}"
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
    return False


def _handle_fail_executor_once(
    req_id: str | None,
    prompt: str,
    *,
    session_file: str | None,
    append_prompt: str,
    tools: str | None,
) -> None:
    """planner 正常成功，executor 首次执行模拟 429 限流失败，带 session_ref 恢复后成功。"""
    is_planner = _is_planner_turn(prompt, append_prompt, tools)
    if is_planner:
        _handle_standard(
            req_id, prompt, tools=tools, append_prompt=append_prompt, session_file=session_file
        )
        return

    history = _load_session_history(session_file)
    prior_exec_turns = [item for item in history if "Currently in the **Plan** phase" not in item]
    _append_session_history(session_file, prompt)
    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )
    if not prior_exec_turns:
        _emit(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "429 rate limit exceeded",
                    "content": [],
                },
            }
        )
        _emit({"type": "agent_settled", "aborted": False})
        return

    final_text = f"EXECUTOR_RECOVERED(turns={len(prior_exec_turns) + 1}):{prompt}"
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


def _handle_interactive_handoff(
    req_id: str | None,
    prompt: str,
    *,
    session_file: str | None,
    append_prompt: str,
    tools: str | None,
) -> None:
    """Interactive multi-turn node scenario supporting optional handoff tool calls."""
    history = _load_session_history(session_file)
    history.append(prompt)
    _append_session_history(session_file, prompt)

    _emit(
        {
            "type": "response",
            "id": req_id,
            "command": "prompt",
            "success": True,
            "data": {"disposition": "started"},
        }
    )

    is_planner = _is_planner_turn(prompt, append_prompt, tools, history)
    if is_planner and ("SUBMIT_PLAN" in prompt or "FAIL_SUBMIT" in prompt):
        is_err = "FAIL_SUBMIT" in prompt
        _emit(
            {
                "type": "tool_execution_start",
                "toolCallId": f"tc-submit-{len(history)}",
                "toolName": "submit_plan",
                "args": {"turn": len(history)},
            }
        )
        _emit(
            {
                "type": "tool_execution_end",
                "toolCallId": f"tc-submit-{len(history)}",
                "toolName": "submit_plan",
                "result": {
                    "content": [{"type": "text", "text": "validation error" if is_err else "ok"}]
                },
                "isError": is_err,
                "durationMs": 5,
            }
        )

    reply = json.dumps(
        {
            "role": "planner" if is_planner else "executor",
            "session_file": session_file,
            "turn_count": len(history),
            "history": history,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    _emit(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": reply, "contentIndex": 0},
        }
    )
    _emit(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": reply}],
            },
        }
    )
    _emit({"type": "agent_settled", "aborted": False})


def main() -> int:
    args = _parse_args()
    append_prompt = _read_text_or_file(args.append_system_prompt)
    if args.session:
        session_file: str | None = args.session
    elif args.fork:
        src = Path(args.fork)
        if not src.is_file():
            sys.stderr.write(f"Source session file not found for --fork: {src}\n")
            sys.stderr.flush()
            return 1
        target_dir = Path(args.session_dir) if args.session_dir else src.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        sess_path = target_dir / f"fake-fork-{uuid4().hex[:10]}.jsonl"
        sess_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        session_file = str(sess_path)
    elif args.no_session:
        session_file = None
    elif args.session_dir:
        sess_path = Path(args.session_dir) / f"fake-{uuid4().hex[:10]}.jsonl"
        sess_path.parent.mkdir(parents=True, exist_ok=True)
        sess_path.touch()
        session_file = str(sess_path)
    else:
        session_file = "fake.jsonl"
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
                _handle_standard(
                    req_id,
                    prompt,
                    tools=args.tools,
                    append_prompt=append_prompt,
                    session_file=session_file,
                )
            elif scenario == "ui_confirm":
                _handle_ui_confirm(req_id)
            elif scenario == "ui_abort_order":
                _handle_ui_abort_order(req_id)
            elif scenario == "abortable":
                _handle_abortable(req_id, prompt, session_file)
            elif scenario == "steerable":
                _handle_steerable(req_id)
            elif scenario == "steer_reject_once":
                _handle_steerable(req_id, reject_first_steer=True)
            elif scenario == "provider_fail_planner":
                _handle_provider_fail_planner(req_id, prompt, append_prompt, args.tools)
            elif scenario == "compaction_race":
                _handle_compaction_race(req_id, prompt, compaction_state)
            elif scenario == "inspect_endpoint":
                _handle_inspect_endpoint(req_id, args)
            elif scenario == "stateful_chat":
                _handle_stateful_chat(req_id, prompt, session_file)
            elif scenario == "interactive_handoff":
                _handle_interactive_handoff(
                    req_id,
                    prompt,
                    session_file=session_file,
                    append_prompt=append_prompt,
                    tools=args.tools,
                )
            elif scenario == "ui_question":
                _handle_ui_question(
                    req_id,
                    prompt,
                    session_file=session_file,
                    append_prompt=append_prompt,
                    tools=args.tools,
                )
            elif scenario == "ui_question_unique":
                _handle_ui_question(
                    req_id,
                    prompt,
                    session_file=session_file,
                    append_prompt=append_prompt,
                    tools=args.tools,
                    question_id=f"ui-q-{uuid4().hex[:8]}",
                )
            elif scenario == "ui_question_crash":
                if _handle_ui_question(
                    req_id,
                    prompt,
                    session_file=session_file,
                    append_prompt=append_prompt,
                    tools=args.tools,
                    crash_while_waiting=True,
                ):
                    return 137
            elif scenario == "fail_executor_once":
                _handle_fail_executor_once(
                    req_id,
                    prompt,
                    session_file=session_file,
                    append_prompt=append_prompt,
                    tools=args.tools,
                )
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
