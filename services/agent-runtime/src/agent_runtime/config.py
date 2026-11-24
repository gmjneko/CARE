"""本地 CARE 配置模块：管理 `~/.care/settings.json`（支持通过 `CARE_HOME` 环境变量覆盖目录）。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.adapters.base import ModelEndpoint, ThinkingLevel
from agent_runtime.paths import (
    CARE_HOME_ENV,
    DEFAULT_SETTINGS_FILENAME,
    resolve_care_home,
    runtime_tmp_dir,
    settings_path,
)


class ModelProfile(ModelEndpoint):
    """持久化在 `~/.care/settings.json` 中的单个命名模型配置。

    直接继承 `ModelEndpoint` 以复用连接字段与构造期校验，
    额外携带该 Profile 默认的 `thinking_level`。
    """

    thinking_level: ThinkingLevel | None = None

    def to_endpoint(self) -> ModelEndpoint:
        """剥离 Profile 附加字段，转为引擎层消费的后端无关 `ModelEndpoint`。"""
        return ModelEndpoint(
            model=self.model,
            provider=self.provider,
            base_url=self.base_url,
            api_protocol=self.api_protocol,
            api_key=self.api_key,
        )


class LocalSettings(BaseModel):
    """本地 `~/.care/settings.json` 对应的配置模型。

    刻意保持默认 `extra="ignore"`，以便后续 Electron 桌面端在同一文件中
    写入主题、窗口状态等 UI 偏好时，不会导致 Python 运行时解析失败。
    """

    model_config = ConfigDict(frozen=True)

    active_profile: str | None = None  # 默认启用的 Profile 名称；None 表示沿用 Pi 本机默认配置
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)  # 按名称索引的模型配置表
    pi_bin: str | None = None  # 可选 Pi 可执行文件路径（解决 GUI 启动时 PATH 缺失问题）

    @model_validator(mode="after")
    def _validate_settings(self) -> LocalSettings:
        if any(not name.strip() for name in self.profiles):
            raise ValueError("Profile names in 'profiles' must be non-empty strings")

        # 若指定了 active_profile，必须在 profiles 中存在，避免运行时静默回退
        if self.active_profile is not None:
            active = self.active_profile.strip()
            if not active:
                raise ValueError("'active_profile' must be non-empty when provided")
            if active not in self.profiles:
                available = ", ".join(sorted(self.profiles)) or "<none>"
                raise ValueError(
                    f"active_profile '{active}' is not defined in 'profiles' "
                    f"(available: {available})"
                )
            # 模型已设为 frozen=True，在校验阶段通过 object.__setattr__ 完成去空白归一化
            object.__setattr__(self, "active_profile", active)

        if self.pi_bin is not None:
            pi_bin = self.pi_bin.strip()
            if not pi_bin:
                raise ValueError("'pi_bin' must be non-empty when provided")
            object.__setattr__(self, "pi_bin", pi_bin)

        return self

    def resolve_profile(self, profile_name: str | None = None) -> ModelProfile | None:
        """按名称解析 Profile；未指定名称时回退到 `active_profile`，均为空则返回 None。"""
        target = profile_name if profile_name is not None else self.active_profile
        if target is None:
            return None
        try:
            return self.profiles[target]
        except KeyError as exc:
            available = ", ".join(sorted(self.profiles)) or "<none>"
            raise KeyError(
                f"Unknown model profile '{target}'. Configured profiles: {available}"
            ) from exc


def load_local_settings(care_home: str | Path | None = None) -> LocalSettings:
    """加载 `<care_home>/settings.json`；文件不存在时返回空默认配置，格式错误则立即报错。"""
    path = settings_path(care_home)
    if not path.exists():
        return LocalSettings()
    return LocalSettings.model_validate_json(path.read_text(encoding="utf-8"))


def save_local_settings(
    settings: LocalSettings,
    *,
    care_home: str | Path | None = None,
) -> Path:
    """以原子替换方式写入 `<care_home>/settings.json`，并在 POSIX 下收紧权限为仅属主读写。"""
    home_dir = resolve_care_home(care_home)
    home_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(home_dir, 0o700)

    target_path = home_dir / DEFAULT_SETTINGS_FILENAME
    payload = _serialize_settings(settings)
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    # 先写入同目录临时文件再 os.replace，避免写入中途异常导致原 settings.json 损坏
    fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=home_dir)
    tmp_path = Path(tmp_name)
    try:
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)  # settings.json 含明文 API Key，限制同机其他用户读取
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        os.replace(tmp_path, target_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return target_path


def _serialize_profile(profile: ModelProfile) -> dict[str, Any]:
    """序列化单条 Profile；仅在写入本地 settings.json 时显式解包 SecretStr 明文。"""
    data = profile.model_dump(mode="json", exclude_none=True)
    if profile.api_key is not None:
        # Pydantic 默认会将 SecretStr 序列化为 "**********"，落盘前需还原真实密钥
        data["api_key"] = profile.api_key.get_secret_value()
    return data


def _serialize_settings(settings: LocalSettings) -> dict[str, Any]:
    """构建写入 settings.json 的顶层字典结构。"""
    data: dict[str, Any] = {
        "active_profile": settings.active_profile,
        "profiles": {
            name: _serialize_profile(profile) for name, profile in settings.profiles.items()
        },
    }
    if settings.pi_bin is not None:
        data["pi_bin"] = settings.pi_bin
    return data


__all__ = [
    "CARE_HOME_ENV",
    "DEFAULT_SETTINGS_FILENAME",
    "LocalSettings",
    "ModelProfile",
    "load_local_settings",
    "resolve_care_home",
    "runtime_tmp_dir",
    "save_local_settings",
    "settings_path",
]
