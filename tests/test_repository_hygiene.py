from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "runtime_path",
    [
        "data/paper_trading.lock",
        "outputs/paper_automation_logs/default-2026-09.log",
        "outputs/paper_automation_runs/default_example.json",
    ],
)
def test_paper_runtime_artifacts_are_ignored_by_git(runtime_path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", runtime_path],
        cwd=PROJECT_ROOT,
        check=False,
    )

    assert result.returncode == 0, f"runtime artifact is not ignored: {runtime_path}"
