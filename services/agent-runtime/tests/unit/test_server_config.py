"""Failure-mode unit tests for ServerConfig and SSE framing/cursor helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from agent_runtime.adapters import ModelEndpoint
from agent_runtime.api import (
    format_sse_event,
    resolve_after_seq,
)
from agent_runtime.config import ResolvedRunModel, ServerConfig, resolve_pi_bin, resolve_run_model
from agent_runtime.protocol import RuntimeErrorEvent, RuntimeEvent, TextDeltaEvent
from agent_runtime.settings import LocalSettings, ModelProfile, save_local_settings


def test_server_config_defaults_and_loopback_hosts() -> None:
    cfg = ServerConfig()
    assert cfg.mode == "local"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8765
    assert cfg.auth_token is None
    assert cfg.sse_keepalive_s == 15.0
    assert cfg.max_retained_runs == 128
    assert cfg.cors_origins == ()

    for loopback in ("127.0.0.1", "localhost", "::1"):
        assert ServerConfig(host=loopback).host == loopback


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"mode": "staging"}, "local"),
        ({"host": "   "}, "host"),
        ({"port": -1}, "port"),
        ({"port": 65536}, "port"),
        ({"sse_keepalive_s": 0}, "sse_keepalive_s"),
        ({"sse_keepalive_s": -2.5}, "sse_keepalive_s"),
        ({"max_retained_runs": 0}, "max_retained_runs"),
        ({"auth_token": SecretStr("   ")}, "auth_token"),
        ({"pi_bin": "   "}, "pi_bin"),
        ({"cors_origins": ("http://localhost:5173", "  ")}, "cors_origins"),
        ({"host": "0.0.0.0"}, "non-loopback"),
        ({"host": "10.0.0.5"}, "non-loopback"),
    ],
)
def test_server_config_rejects_invalid_values(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ServerConfig(**kwargs)  # type: ignore[arg-type]


def test_server_config_allows_non_loopback_with_token_and_masks_repr() -> None:
    cfg = ServerConfig(
        mode="cloud",
        host="0.0.0.0",
        port=9000,
        auth_token=SecretStr("  secret-sidecar-token  "),
    )
    assert cfg.auth_token is not None
    assert cfg.auth_token.get_secret_value() == "secret-sidecar-token"
    assert "secret-sidecar-token" not in repr(cfg)


def test_server_config_from_env_parses_all_fields() -> None:
    cfg = ServerConfig.from_env(
        {
            "MODE": "cloud",
            "CARE_RUNTIME_HOST": "0.0.0.0",
            "CARE_RUNTIME_PORT": "9123",
            "RUNTIME_AUTH_TOKEN": "tok-123",
            "CARE_CHECKPOINT_DB": ":memory:",
            "PI_BIN": "/usr/local/bin/pi",
            "CARE_SSE_KEEPALIVE_S": "5.5",
            "CARE_MAX_RETAINED_RUNS": "64",
            "CARE_CORS_ORIGINS": "http://localhost:5173, app://care",
        }
    )
    assert cfg.mode == "cloud"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9123
    assert cfg.auth_token is not None and cfg.auth_token.get_secret_value() == "tok-123"
    assert cfg.checkpoint_db == ":memory:"
    assert cfg.pi_bin == "/usr/local/bin/pi"
    assert cfg.sse_keepalive_s == 5.5
    assert cfg.max_retained_runs == 64
    assert cfg.cors_origins == ("http://localhost:5173", "app://care")

    cfg_file = ServerConfig.from_env({"CARE_CHECKPOINT_DB": "/tmp/custom.sqlite"})
    assert cfg_file.checkpoint_db == Path("/tmp/custom.sqlite")


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"CARE_RUNTIME_PORT": "not-a-port"}, "CARE_RUNTIME_PORT"),
        ({"CARE_SSE_KEEPALIVE_S": "fast"}, "CARE_SSE_KEEPALIVE_S"),
        ({"CARE_MAX_RETAINED_RUNS": "many"}, "CARE_MAX_RETAINED_RUNS"),
        ({"RUNTIME_AUTH_TOKEN": "   "}, "RUNTIME_AUTH_TOKEN"),
        ({"CARE_RUNTIME_HOST": "0.0.0.0"}, "non-loopback"),
    ],
)
def test_server_config_from_env_fails_fast(env: dict[str, str], match: str) -> None:
    with pytest.raises((ValueError, ValidationError), match=match):
        ServerConfig.from_env(env)


def test_resolve_after_seq_valid_and_failure_modes() -> None:
    assert resolve_after_seq(None, None) == 0
    assert resolve_after_seq(None, "   ") == 0
    assert resolve_after_seq(None, " 12 ") == 12
    assert resolve_after_seq(5, "99") == 5
    assert resolve_after_seq(0, "99") == 0

    with pytest.raises(ValueError, match="after_seq"):
        resolve_after_seq(-1, None)
    with pytest.raises(ValueError, match="Last-Event-ID"):
        resolve_after_seq(None, "-3")
    with pytest.raises(ValueError, match="Last-Event-ID"):
        resolve_after_seq(None, "abc")
    with pytest.raises(ValueError, match="Last-Event-ID"):
        resolve_after_seq(None, "1.5")


def test_sse_formatting_and_error_redaction() -> None:
    event = RuntimeEvent(
        seq=7,
        run_id="run-1",
        thread_id="thread-1",
        workflow_id="single_agent",
        node_id="agent",
        payload=TextDeltaEvent(delta="hello"),
    )
    frame = format_sse_event(event)
    assert frame.startswith("id: 7\nevent: text.delta\ndata: ")
    assert frame.endswith("\n\n")
    assert '"delta":"hello"' in frame
    assert '"is_ephemeral":true' in frame

    err_event = RuntimeEvent(
        seq=8,
        run_id="run-1",
        thread_id="thread-1",
        workflow_id="single_agent",
        payload=RuntimeErrorEvent.from_raw(
            code="adapter_error",
            message=(
                "Upstream failed with Authorization: Bearer topsecret123 "
                "and sk-1234567890abcdef1234567890"
            ),
        ),
    )
    err_frame = format_sse_event(err_event)
    assert err_frame.startswith("id: 8\nevent: runtime.error\ndata: ")
    assert err_frame.endswith("\n\n")
    assert '"is_ephemeral":false' in err_frame
    assert "topsecret123" not in err_frame
    assert "sk-1234567890abcdef1234567890" not in err_frame


def test_resolve_pi_bin_precedence_and_cloud_isolation(tmp_path: Path) -> None:
    save_local_settings(
        LocalSettings(adapter_bins={"pi": "/settings/pi"}),
        care_home=tmp_path,
    )
    assert (
        resolve_pi_bin(
            mode="local",
            pi_bin="  /explicit/pi  ",
            environ={"PI_BIN": "/env/pi"},
            care_home=tmp_path,
        )
        == "/explicit/pi"
    )
    assert (
        resolve_pi_bin(
            mode="local",
            environ={"PI_BIN": " /env/pi "},
            care_home=tmp_path,
        )
        == "/env/pi"
    )
    assert (
        resolve_pi_bin(mode="local", environ={}, care_home=tmp_path)
        == "/settings/pi"
    )
    assert (
        resolve_pi_bin(mode="cloud", environ={}, care_home=tmp_path)
        == "pi"
    )
    with pytest.raises(ValueError, match="pi_bin"):
        resolve_pi_bin(mode="local", pi_bin="   ", environ={}, care_home=tmp_path)


def test_resolve_run_model_rules(tmp_path: Path) -> None:
    save_local_settings(
        LocalSettings(
            active_profile="relay",
            profiles={
                "relay": ModelProfile(
                    model="deepseek-chat",
                    base_url="https://relay.example.com/v1",
                    thinking_level="medium",
                )
            },
        ),
        care_home=tmp_path,
    )
    resolved = resolve_run_model(mode="local", care_home=tmp_path)
    assert resolved.profile == "relay"
    assert resolved.endpoint is not None and resolved.endpoint.model == "deepseek-chat"
    assert resolved.thinking_level == "medium"

    inline = ModelEndpoint(model="gpt-4o", base_url="https://api.example.com/v1")
    with pytest.raises(ValueError, match="both 'profile' and 'endpoint'"):
        resolve_run_model(mode="local", profile="relay", endpoint=inline, care_home=tmp_path)
    with pytest.raises(ValueError, match="local mode"):
        resolve_run_model(mode="cloud", profile="relay", care_home=tmp_path)
    assert resolve_run_model(
        mode="cloud", endpoint=inline, thinking_level="high"
    ) == ResolvedRunModel(profile=None, endpoint=inline, thinking_level="high")
