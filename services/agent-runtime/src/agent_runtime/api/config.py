"""Server configuration and local/cloud model resolution for the FastAPI boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from agent_runtime.adapters import ModelEndpoint, ThinkingLevel
from agent_runtime.config import load_local_settings

RuntimeMode = Literal["local", "cloud"]

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ServerConfig(BaseModel):
    """Runtime server configuration supporting local Sidecar and cloud sandbox modes."""

    model_config = ConfigDict(frozen=True)

    mode: RuntimeMode = "local"
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: SecretStr | None = None
    checkpoint_db: Path | Literal[":memory:"] | None = None
    pi_bin: str | None = None
    sse_keepalive_s: float = 15.0
    max_retained_runs: int = 128
    cors_origins: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_config(self) -> ServerConfig:
        host = self.host.strip()
        if not host:
            raise ValueError("'host' must be a non-empty string")
        object.__setattr__(self, "host", host)

        if not (0 <= self.port <= 65535):
            raise ValueError("'port' must be between 0 and 65535")
        if self.sse_keepalive_s <= 0:
            raise ValueError("'sse_keepalive_s' must be greater than 0")
        if self.max_retained_runs < 1:
            raise ValueError("'max_retained_runs' must be at least 1")

        if self.auth_token is not None:
            secret = self.auth_token.get_secret_value().strip()
            if not secret:
                raise ValueError("'auth_token' must be non-empty when provided")
            object.__setattr__(self, "auth_token", SecretStr(secret))

        if host not in _LOOPBACK_HOSTS and self.auth_token is None:
            raise ValueError(
                f"Binding to non-loopback host '{host}' requires 'auth_token' (RUNTIME_AUTH_TOKEN)"
            )

        if self.pi_bin is not None:
            clean_bin = self.pi_bin.strip()
            if not clean_bin:
                raise ValueError("'pi_bin' must be non-empty when provided")
            object.__setattr__(self, "pi_bin", clean_bin)

        cleaned_origins: list[str] = []
        for origin in self.cors_origins:
            clean = origin.strip()
            if not clean:
                raise ValueError("'cors_origins' entries must be non-empty strings")
            cleaned_origins.append(clean)
        object.__setattr__(self, "cors_origins", tuple(cleaned_origins))

        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ServerConfig:
        """Build ServerConfig from environment variables, failing fast on invalid values."""
        env = os.environ if environ is None else environ
        raw_mode = (env.get("CARE_RUNTIME_MODE") or env.get("MODE") or "local").strip()
        host = (env.get("CARE_RUNTIME_HOST") or "127.0.0.1").strip()

        port = _parse_int_env("CARE_RUNTIME_PORT", env.get("CARE_RUNTIME_PORT"), default=8765)

        auth_token: SecretStr | None = None
        if "RUNTIME_AUTH_TOKEN" in env:
            token_val = env["RUNTIME_AUTH_TOKEN"].strip()
            if not token_val:
                raise ValueError("RUNTIME_AUTH_TOKEN must be non-empty when set")
            auth_token = SecretStr(token_val)

        raw_db = env.get("CARE_CHECKPOINT_DB")
        checkpoint_db: Path | Literal[":memory:"] | None = None
        if raw_db is not None and raw_db.strip():
            trimmed_db = raw_db.strip()
            checkpoint_db = (
                ":memory:" if trimmed_db == ":memory:" else Path(trimmed_db).expanduser()
            )

        raw_pi_bin = env.get("PI_BIN")
        pi_bin = raw_pi_bin.strip() if raw_pi_bin and raw_pi_bin.strip() else None

        sse_keepalive_s = _parse_float_env(
            "CARE_SSE_KEEPALIVE_S", env.get("CARE_SSE_KEEPALIVE_S"), default=15.0
        )
        max_retained_runs = _parse_int_env(
            "CARE_MAX_RETAINED_RUNS", env.get("CARE_MAX_RETAINED_RUNS"), default=128
        )

        raw_cors = env.get("CARE_CORS_ORIGINS")
        cors_origins: tuple[str, ...] = ()
        if raw_cors is not None and raw_cors.strip():
            cors_origins = tuple(item.strip() for item in raw_cors.split(","))

        return cls(
            mode=raw_mode,  # type: ignore[arg-type]
            host=host,
            port=port,
            auth_token=auth_token,
            checkpoint_db=checkpoint_db,
            pi_bin=pi_bin,
            sse_keepalive_s=sse_keepalive_s,
            max_retained_runs=max_retained_runs,
            cors_origins=cors_origins,
        )


def resolve_run_model(
    *,
    mode: RuntimeMode,
    profile: str | None,
    endpoint: ModelEndpoint | None,
    thinking_level: ThinkingLevel | None,
) -> tuple[ModelEndpoint | None, ThinkingLevel | None]:
    """Resolve effective ModelEndpoint and thinking level according to local/cloud mode rules."""
    if mode == "cloud":
        if profile is not None:
            raise ValueError("'profile' is only supported in local mode; pass 'endpoint' directly")
        return endpoint, thinking_level
    if endpoint is not None:
        return endpoint, thinking_level
    settings = load_local_settings()
    resolved = settings.resolve_profile(profile)
    if resolved is None:
        return None, thinking_level
    effective_thinking = thinking_level if thinking_level is not None else resolved.thinking_level
    return resolved.to_endpoint(), effective_thinking


def resolve_default_pi_bin(config: ServerConfig) -> str:
    """Resolve the default Pi executable path for server startup."""
    if config.pi_bin is not None:
        return config.pi_bin
    if config.mode == "local":
        configured = load_local_settings().resolve_adapter_bin("pi")
        if configured is not None:
            return configured
    return os.environ.get("PI_BIN", "pi")


def _parse_int_env(name: str, raw: str | None, *, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid integer for environment variable '{name}': {raw!r}") from exc


def _parse_float_env(name: str, raw: str | None, *, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid float for environment variable '{name}': {raw!r}") from exc


__all__ = [
    "RuntimeMode",
    "ServerConfig",
    "resolve_default_pi_bin",
    "resolve_run_model",
]
