from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_posix_script(relative_path: str) -> str:
    path = PROJECT_ROOT / relative_path
    assert path.is_file(), f"missing POSIX entrypoint: {relative_path}"
    payload = path.read_bytes()
    assert payload.startswith(b"#!/usr/bin/env sh\n")
    assert b"\r\n" not in payload
    return payload.decode("utf-8")


def test_posix_demo_entrypoints_are_portable_and_fail_fast() -> None:
    root_launcher = _read_posix_script("run_demo.sh")
    setup_script = _read_posix_script("scripts/setup.sh")
    demo_script = _read_posix_script("scripts/run_demo.sh")

    assert "set -eu" in root_launcher
    assert 'exec "$SCRIPT_DIR/scripts/run_demo.sh"' in root_launcher

    assert "set -eu" in setup_script
    assert ".venv/bin/python" in setup_script
    assert "3.11" in setup_script and "3.12" in setup_script and "3.13" in setup_script
    assert "requirements-lock.txt" in setup_script
    assert "--no-build-isolation --no-deps -e ." in setup_script

    assert "set -eu" in demo_script
    assert '"$SCRIPT_DIR/setup.sh"' in demo_script
    assert "sys.version_info" in demo_script
    assert '"$VENV_PYTHON" -m finance_lab.cli demo' in demo_script
    assert "demo_000300_sma_report.html" in demo_script


def test_release_package_requires_posix_demo_entrypoints() -> None:
    package_script = (PROJECT_ROOT / "scripts" / "package_release.ps1").read_text(
        encoding="utf-8"
    )

    assert package_script.count('"run_demo.sh"') >= 2
    assert '"scripts\\setup.sh"' in package_script
    assert '"scripts\\run_demo.sh"' in package_script


def test_ci_runs_the_demo_on_linux_and_macos() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "posix-demo:" in workflow
    assert "ubuntu-latest" in workflow
    assert "macos-latest" in workflow
    assert "run: ./run_demo.sh" in workflow
    assert "test -f outputs/demo_000300_sma_report.html" in workflow


def test_git_keeps_shell_entrypoints_with_lf_line_endings() -> None:
    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "*.sh text eol=lf" in attributes.splitlines()
