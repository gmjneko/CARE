"""开发期本地运行时状态清理脚本（保留 `~/.care/settings.json` 模型配置）。

默认清理：
- `<CARE_HOME>/checkpoints.sqlite`（含 `-wal` / `-shm` 伴生文件）
- `<CARE_HOME>/tmp/` 残留临时目录

可选传入 `--include-sessions` 一并清理 `<CARE_HOME>/sessions/`。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from agent_runtime.paths import (
    checkpoints_db_path,
    resolve_care_home,
    runtime_tmp_dir,
    sessions_dir,
)


def reset_local_state(
    *,
    care_home: str | Path | None = None,
    include_sessions: bool = False,
) -> list[Path]:
    """清理本地开发期运行时状态文件，返回实际删除的路径列表（绝不删除 settings.json）。"""
    removed: list[Path] = []
    db_path = checkpoints_db_path(care_home)
    for candidate in (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ):
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate)

    tmp_dir = runtime_tmp_dir(care_home)
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir)
        removed.append(tmp_dir)

    if include_sessions:
        sess_dir = sessions_dir(care_home)
        if sess_dir.is_dir():
            shutil.rmtree(sess_dir)
            removed.append(sess_dir)

    return removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset local CARE agent-runtime checkpoints/tmp state (keeps settings.json)."
    )
    parser.add_argument(
        "--care-home",
        default=None,
        help="Override CARE_HOME directory (defaults to $CARE_HOME or ~/.care).",
    )
    parser.add_argument(
        "--include-sessions",
        action="store_true",
        help="Also delete persisted Pi session JSONL files under sessions/.",
    )
    args = parser.parse_args()

    home = resolve_care_home(args.care_home)
    removed = reset_local_state(
        care_home=args.care_home,
        include_sessions=args.include_sessions,
    )
    if not removed:
        print(f"Nothing to clean under {home}")
        return 0
    for path in removed:
        print(f"Removed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
