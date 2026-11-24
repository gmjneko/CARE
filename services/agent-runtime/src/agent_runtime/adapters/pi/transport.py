"""Subprocess and JSONL RPC transport for `pi --mode rpc`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_runtime.adapters.base import SessionConfig
from agent_runtime.adapters.errors import (
    AdapterError,
    AdapterProtocolError,
    AdapterRpcError,
    AdapterStartupError,
    AdapterTransportError,
)
from agent_runtime.adapters.pi.framing import DEFAULT_MAX_LINE_BYTES, JsonlFramer
from agent_runtime.protocol.events import redact_sensitive_text

_STDERR_MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class TransportClosed:
    """Sentinel placed on inbound_queue when the RPC transport terminates."""

    error: AdapterError | None = None


class PiRpcConfig(BaseModel):
    """Configuration for spawning and communicating with a Pi RPC subprocess."""

    model_config = ConfigDict(frozen=True)

    # Injectable external boundary seam: defaults to the installed `pi` CLI.
    command: tuple[str, ...] = ("pi",)
    request_timeout_s: float = 30.0
    shutdown_grace_s: float = 1.0
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    offline: bool = False


def resolve_pi_executable(executable: str) -> str:
    """Resolve `pi` or custom executable path, failing fast if unavailable."""
    candidate_path = Path(executable)
    if candidate_path.is_file():
        return str(candidate_path)

    found = shutil.which(executable)
    if found:
        return found

    # Official Pi installer places the shim in ~/.pi/agent/bin; check it if the
    # current shell inherited a stale PATH before Pi was installed.
    if executable == "pi":
        home_bin = Path.home() / ".pi" / "agent" / "bin"
        candidates = (
            [home_bin / "pi.cmd", home_bin / "pi.exe", home_bin / "pi"]
            if os.name == "nt"
            else [home_bin / "pi"]
        )
        for path in candidates:
            if path.is_file():
                return str(path)

    raise AdapterStartupError(
        f"Executable '{executable}' was not found on PATH. "
        "Install Pi CLI (`@earendil-works/pi-coding-agent`) or pass an explicit command."
    )


def build_pi_argv(
    *,
    base_command: tuple[str, ...],
    session_config: SessionConfig,
    prompt_dir: Path,
    offline: bool = False,
) -> list[str]:
    """Construct `pi --mode rpc` argv, staging prompts via temp files to avoid shell quirks."""
    if not base_command:
        raise AdapterStartupError("PiRpcConfig.command must contain at least one element")

    resolved_exe = resolve_pi_executable(base_command[0])
    argv: list[str] = [resolved_exe, *base_command[1:], "--mode", "rpc"]

    if session_config.session_ref:
        argv.extend(["--session", session_config.session_ref])
    elif not session_config.persist_session:
        argv.append("--no-session")

    if offline:
        argv.append("--offline")
    if session_config.provider:
        argv.extend(["--provider", session_config.provider])
    if session_config.model:
        argv.extend(["--model", session_config.model])
    if session_config.thinking_level:
        argv.extend(["--thinking", session_config.thinking_level])

    if session_config.system_prompt is not None:
        sys_file = prompt_dir / "system_prompt.txt"
        sys_file.write_text(session_config.system_prompt, encoding="utf-8")
        argv.extend(["--system-prompt", str(sys_file)])

    if session_config.append_system_prompt is not None:
        append_file = prompt_dir / "append_system_prompt.txt"
        append_file.write_text(session_config.append_system_prompt, encoding="utf-8")
        argv.extend(["--append-system-prompt", str(append_file)])

    if session_config.no_tools:
        argv.append("--no-tools")
    elif session_config.tools is not None:
        argv.extend(["--tools", ",".join(session_config.tools)])

    for ext in session_config.extensions:
        argv.extend(["--extension", str(ext)])

    return argv


class PiRpcTransport:
    """Manages a single `pi --mode rpc` subprocess and JSONL bidirectional I/O."""

    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        prompt_dir: tempfile.TemporaryDirectory[str],
        config: PiRpcConfig,
    ) -> None:
        self._process = process
        self._prompt_dir = prompt_dir
        self._config = config
        self._write_lock = asyncio.Lock()
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._next_request_id = 0
        self._closing = False
        self._closed = False
        self._terminal_error: AdapterError | None = None
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_bytes = 0
        self.inbound_queue: asyncio.Queue[dict[str, Any] | TransportClosed] = asyncio.Queue()

        self._stdout_task = asyncio.create_task(self._read_stdout(), name="pi-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="pi-rpc-stderr")

    @classmethod
    async def spawn(
        cls,
        *,
        rpc_config: PiRpcConfig,
        session_config: SessionConfig,
    ) -> PiRpcTransport:
        if not session_config.cwd.exists():
            raise AdapterStartupError(f"Working directory does not exist: {session_config.cwd}")

        prompt_dir = tempfile.TemporaryDirectory(prefix="care-pi-")
        try:
            argv = build_pi_argv(
                base_command=rpc_config.command,
                session_config=session_config,
                prompt_dir=Path(prompt_dir.name),
                offline=rpc_config.offline,
            )
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(session_config.cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # On POSIX, isolate Pi in its own process group so close() reaps descendants.
                start_new_session=(os.name != "nt"),
            )
        except AdapterError:
            prompt_dir.cleanup()
            raise
        except OSError as exc:
            prompt_dir.cleanup()
            raise AdapterStartupError(f"Failed to spawn Pi RPC process: {exc}") from exc

        return cls(process=process, prompt_dir=prompt_dir, config=rpc_config)

    def next_id(self, prefix: str = "care") -> str:
        self._next_request_id += 1
        return f"{prefix}-{self._next_request_id}"

    async def send(self, record: dict[str, Any]) -> None:
        """Write a single JSONL record without waiting for a response frame."""
        if self._closed or self._terminal_error is not None:
            raise self._terminal_error or AdapterTransportError("Pi RPC transport is closed")
        assert self._process.stdin is not None
        encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                err = AdapterTransportError(f"Failed writing to Pi RPC stdin: {exc}")
                err.__cause__ = exc
                self._fail_transport(err)
                raise err from exc

    async def request(
        self,
        command: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send an ID-correlated control command and await its matching RPC response."""
        req_id = command.get("id")
        if not isinstance(req_id, str) or not req_id:
            req_id = self.next_id("rpc")
            payload = {**command, "id": req_id}
        else:
            payload = command

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future

        try:
            await self.send(payload)
            effective_timeout = timeout_s or self._config.request_timeout_s
            response = await asyncio.wait_for(future, timeout=effective_timeout)
        except TimeoutError as exc:
            self._pending_requests.pop(req_id, None)
            cmd_name = str(payload.get("type", "unknown"))
            raise AdapterTransportError(
                f"Timed out waiting for Pi RPC '{cmd_name}' response after {effective_timeout}s"
            ) from exc
        except BaseException:
            self._pending_requests.pop(req_id, None)
            raise

        if not response.get("success", False):
            cmd_name = str(response.get("command") or payload.get("type") or "unknown")
            detail = str(response.get("error") or "Unknown RPC error")
            raise AdapterRpcError(cmd_name, detail)

        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def close(self) -> None:
        """Terminate the Pi subprocess tree cleanly and release resources."""
        if self._closed:
            return
        self._closing = True
        try:
            await self._terminate_process_tree()
        finally:
            self._closed = True
            self._stdout_task.cancel()
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(self._stdout_task, self._stderr_task, return_exceptions=True)
            if self._process.stdin is not None and not self._process.stdin.is_closing():
                self._process.stdin.close()
            self._fail_pending(AdapterTransportError("Pi RPC transport closed"))
            self.inbound_queue.put_nowait(TransportClosed(error=None))
            self._prompt_dir.cleanup()

    async def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        framer = JsonlFramer(max_line_bytes=self._config.max_line_bytes)
        try:
            while True:
                chunk = await self._process.stdout.read(65536)
                if not chunk:
                    break
                for record in framer.feed(chunk):
                    self._route_record(record)
            framer.finish()
        except AdapterProtocolError as exc:
            self._fail_transport(exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary conversion
            err = AdapterTransportError(f"Unexpected Pi stdout reader failure: {exc}")
            err.__cause__ = exc
            self._fail_transport(err)
            return

        if self._closing:
            self.inbound_queue.put_nowait(TransportClosed(error=None))
            return

        # Wait briefly for exit code so diagnostics reflect process termination rather than EOF.
        try:
            exit_code = await asyncio.wait_for(self._process.wait(), timeout=0.5)
        except TimeoutError:
            exit_code = self._process.returncode

        stderr_summary = self._stderr_diagnostic()
        detail = f"Pi RPC process exited unexpectedly (code={exit_code})"
        if stderr_summary:
            detail = f"{detail}; stderr({self._stderr_bytes}B): {stderr_summary}"
        self._fail_transport(AdapterTransportError(detail))

    async def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        try:
            while True:
                chunk = await self._process.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_bytes += len(chunk)
                self._stderr_chunks.append(chunk)
                # Keep only the most recent bounded tail in memory.
                while sum(len(c) for c in self._stderr_chunks) > _STDERR_MAX_BYTES:
                    self._stderr_chunks.popleft()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _stderr_diagnostic(self) -> str:
        if not self._stderr_chunks:
            return ""
        raw = b"".join(self._stderr_chunks).decode("utf-8", errors="replace")
        return redact_sensitive_text(raw, max_chars=512)

    def _route_record(self, record: dict[str, Any]) -> None:
        if record.get("type") == "response":
            req_id = record.get("id")
            if isinstance(req_id, str):
                future = self._pending_requests.pop(req_id, None)
                if future is not None:
                    if not future.done():
                        future.set_result(record)
                    return
        self.inbound_queue.put_nowait(record)

    def _fail_pending(self, error: AdapterError) -> None:
        for future in list(self._pending_requests.values()):
            if not future.done():
                future.set_exception(error)
        self._pending_requests.clear()

    def _fail_transport(self, error: AdapterError) -> None:
        if self._terminal_error is None:
            self._terminal_error = error
        self._fail_pending(self._terminal_error)
        self.inbound_queue.put_nowait(TransportClosed(error=self._terminal_error))

    async def _terminate_process_tree(self) -> None:
        if self._process.returncode is not None:
            return

        pid = self._process.pid
        if os.name == "nt":
            # Windows process.terminate() only stops cmd.exe when invoked via pi.cmd,
            # leaving Node and tool children alive. `taskkill /T /F` reaps the whole tree.
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            return

        # POSIX: signal the process group created via start_new_session=True.
        if pid > 1:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGTERM)
        else:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()

        try:
            await asyncio.wait_for(self._process.wait(), timeout=self._config.shutdown_grace_s)
        except TimeoutError:
            if self._process.returncode is None:
                if pid > 1:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)
                else:
                    with contextlib.suppress(ProcessLookupError):
                        self._process.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._process.wait(), timeout=1.0)
