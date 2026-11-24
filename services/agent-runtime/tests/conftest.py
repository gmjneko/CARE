"""Pytest 全局夹具：将测试期间的 CARE_HOME 隔离到临时目录，避免污染本机 ~/.care。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_care_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARE_HOME", str(tmp_path / ".care-test-home"))
