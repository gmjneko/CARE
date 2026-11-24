"""本地 CARE 目录路径约定（无内部模块依赖，供 config 与 adapters 共用以避免循环导入）。"""

from __future__ import annotations

import os
from pathlib import Path

CARE_HOME_ENV = "CARE_HOME"
DEFAULT_SETTINGS_FILENAME = "settings.json"
DEFAULT_TMP_DIRNAME = "tmp"


def resolve_care_home(care_home: str | Path | None = None) -> Path:
    """解析本地 CARE 主目录，优先级：显式参数 > 环境变量 `CARE_HOME` > `~/.care`。"""
    if care_home is not None:
        return Path(care_home).expanduser()
    env_home = os.environ.get(CARE_HOME_ENV)
    if env_home and env_home.strip():
        return Path(env_home.strip()).expanduser()
    return Path.home() / ".care"


def settings_path(care_home: str | Path | None = None) -> Path:
    """返回 `<care_home>/settings.json` 的完整路径。"""
    return resolve_care_home(care_home) / DEFAULT_SETTINGS_FILENAME


def runtime_tmp_dir(care_home: str | Path | None = None) -> Path:
    """返回 `<care_home>/tmp` 路径，用于统一收口子进程瞬态运行目录。"""
    return resolve_care_home(care_home) / DEFAULT_TMP_DIRNAME


__all__ = [
    "CARE_HOME_ENV",
    "DEFAULT_SETTINGS_FILENAME",
    "DEFAULT_TMP_DIRNAME",
    "resolve_care_home",
    "runtime_tmp_dir",
    "settings_path",
]
