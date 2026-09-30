from __future__ import annotations

import subprocess
import tomllib
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


def test_package_version_has_one_code_source_of_truth() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    setuptools = pyproject["tool"]["setuptools"]
    package_script = (PROJECT_ROOT / "scripts" / "package_release.ps1").read_text(
        encoding="utf-8"
    )

    assert "version" not in project
    assert "version" in project["dynamic"]
    assert setuptools["dynamic"]["version"]["attr"] == "finance_lab.__version__"
    assert '[string]$Version = "v0.9.1"' not in package_script
    assert "from finance_lab import __version__" in package_script


def test_ci_enforces_v1_quality_gates() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "--cov-fail-under=80" in workflow
    assert "python -m pip_audit" in workflow
    assert "python -m build --no-isolation" in workflow
    assert "smoke-venv" in workflow
    assert "scripts/check_markdown_links.py" in workflow
