"""Failure-mode unit tests for ModelEndpoint and ~/.care/settings.json persistence."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_runtime.adapters import ModelEndpoint, SessionConfig
from agent_runtime.paths import (
    CARE_HOME_ENV,
    checkpoints_db_path,
    resolve_care_home,
    runtime_tmp_dir,
    sessions_dir,
    settings_path,
)
from agent_runtime.settings import (
    LocalSettings,
    ModelProfile,
    load_local_settings,
    save_local_settings,
)


def test_missing_settings_file_returns_empty_defaults(tmp_path: Path) -> None:
    loaded = load_local_settings(care_home=tmp_path / "missing-home")
    assert loaded.active_profile is None
    assert loaded.profiles == {}
    assert loaded.adapter_bins == {}
    assert loaded.resolve_adapter_bin("pi") is None
    assert loaded.resolve_profile() is None


def test_care_home_env_overrides_default_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CARE_HOME_ENV, raising=False)
    assert resolve_care_home() == Path.home() / ".care"
    assert runtime_tmp_dir() == Path.home() / ".care" / "tmp"
    assert sessions_dir() == Path.home() / ".care" / "sessions"
    assert checkpoints_db_path() == Path.home() / ".care" / "checkpoints.sqlite"

    custom_home = tmp_path / "custom-care"
    monkeypatch.setenv(CARE_HOME_ENV, str(custom_home))
    assert resolve_care_home() == custom_home
    assert settings_path() == custom_home / "settings.json"
    assert runtime_tmp_dir() == custom_home / "tmp"
    assert sessions_dir() == custom_home / "sessions"
    assert checkpoints_db_path() == custom_home / "checkpoints.sqlite"


def test_save_and_load_round_trip_masks_secret_in_memory_and_sets_permissions(
    tmp_path: Path,
) -> None:
    secret = "sk-local-roundtrip-secret"
    original = LocalSettings(
        active_profile="deepseek",
        adapter_bins={"pi": "  /opt/homebrew/bin/pi  "},
        profiles={
            "deepseek": ModelProfile(
                model="  deepseek-chat  ",
                base_url="  https://api.deepseek.com/v1  ",
                api_key=f"  {secret}  ",
                thinking_level="medium",
            ),
            "ollama": ModelProfile(
                model="qwen2.5-coder:7b",
                base_url="http://localhost:11434/v1",
            ),
        },
    )

    path = save_local_settings(original, care_home=tmp_path)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700

    # Disk file stores usable plaintext key, while Pydantic in-memory dumps keep it masked.
    raw_disk = json.loads(path.read_text(encoding="utf-8"))
    assert raw_disk["profiles"]["deepseek"]["api_key"] == secret

    loaded = load_local_settings(care_home=tmp_path)
    profile = loaded.resolve_profile()
    assert profile is not None
    assert profile.model == "deepseek-chat"
    assert profile.base_url == "https://api.deepseek.com/v1"
    assert profile.api_key is not None
    assert profile.api_key.get_secret_value() == secret
    assert secret not in repr(profile)
    assert secret not in profile.model_dump_json()
    assert loaded.resolve_profile("ollama") == loaded.profiles["ollama"]
    assert loaded.resolve_adapter_bin("pi") == "/opt/homebrew/bin/pi"
    assert loaded.resolve_adapter_bin("codex", default="codex") == "codex"


def test_extra_desktop_keys_in_settings_json_are_tolerated(tmp_path: Path) -> None:
    path = settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "window": {"width": 1280},
                "active_profile": "default",
                "profiles": {
                    "default": {
                        "model": "gpt-4o",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-123",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_local_settings(care_home=tmp_path)
    profile = loaded.resolve_profile()
    assert profile is not None
    assert profile.model == "gpt-4o"


def test_malformed_settings_json_fails_fast(tmp_path: Path) -> None:
    path = settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_local_settings(care_home=tmp_path)


def test_active_profile_missing_from_profiles_fails_fast() -> None:
    with pytest.raises(ValidationError, match="active_profile"):
        LocalSettings(active_profile="missing", profiles={})


def test_resolve_unknown_profile_raises_key_error() -> None:
    settings = LocalSettings()
    with pytest.raises(KeyError, match="Unknown model profile 'missing'"):
        settings.resolve_profile("missing")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"model": "   "}, "model"),
        ({"model": "gpt-4o", "provider": "   "}, "provider"),
        ({"model": "gpt-4o", "base_url": "api.openai.com/v1"}, "http://"),
        ({"model": "gpt-4o", "base_url": "https://api.openai.com/v1", "api_key": "   "}, "api_key"),
        ({"model": "gpt-4o", "api_key": "sk-only"}, "requires 'provider'"),
        ({"model": "gpt-4o", "api_protocol": "unsupported"}, "api_protocol"),
    ],
)
def test_invalid_model_endpoint_fails_fast(kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ModelEndpoint(**kwargs)


def test_invalid_adapter_bins_and_session_tool_policy_fail_fast(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="adapter_bins"):
        LocalSettings(adapter_bins={"  ": "/usr/bin/pi"})
    with pytest.raises(ValidationError, match="Executable path"):
        LocalSettings(adapter_bins={"pi": "   "})
    with pytest.raises(ValidationError, match="tool_policy"):
        SessionConfig(cwd=tmp_path, tool_policy="read_only", tools=("read",))
    with pytest.raises(ValidationError, match="persist_session"):
        SessionConfig(cwd=tmp_path, session_ref="s1.jsonl", persist_session=False)
    with pytest.raises(ValidationError, match="persist_session"):
        SessionConfig(cwd=tmp_path, fork_from_session_ref="s1.jsonl", persist_session=False)
    with pytest.raises(ValidationError, match="fork_from_session_ref"):
        SessionConfig(
            cwd=tmp_path,
            session_ref="s1.jsonl",
            fork_from_session_ref="s0.jsonl",
            persist_session=True,
        )
