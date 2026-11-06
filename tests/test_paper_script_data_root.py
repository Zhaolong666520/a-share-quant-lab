"""Real Windows PowerShell routing; only interpreter/UI/task API boundaries isolated."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell required")


def _quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ps(
    command: str, cwd: Path, env_root: str | None = None, *, unset_env: bool = False
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUTF8"] = "0"
    if unset_env:
        env.pop("FINANCE_LAB_ROOT", None)
    else:
        if env_root is None:
            safety = cwd / "process-safe-root"
            if not safety.exists():
                _marker(safety)
            env_root = str(safety)
        env["FINANCE_LAB_ROOT"] = env_root
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )


def _marker(root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "instruments.json").write_text(
        json.dumps([{"code": "510300", "exchange": "sh", "name": "test", "kind": "etf"}]),
        encoding="utf-8",
    )
    (root / "data" / "market_update_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": "a" * 32,
                "status": "updating",
                "start": "2022-01-01",
                "end": "2022-07-15",
                "symbols": ["sh.510300"],
                "started_at": "2022-07-15T10:00:00+00:00",
                "phase": "curated",
                "completed_at": None,
                "summary_sha256": None,
                "error_type": None,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def scripts(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "maintenance"
    directory = project / "scripts"
    directory.mkdir(parents=True)
    for name in ("run_paper_daily.ps1", "install_paper_daily_task.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")
        text = text.replace(
            '$Python = Join-Path $ProjectRoot ".venv\\Scripts\\python.exe"',
            "$Python = " + _quote(sys.executable),
        )
        # Notification UI is not part of routing. No root/env/log/CLI mocks.
        text = text.replace(
            "    Show-PaperFailureNotification $message", "    # UI suppressed in test copy"
        )
        (directory / name).write_text(text, encoding="utf-8-sig")
    return project, directory


def _invoke_runner(
    scripts: tuple[Path, Path],
    tmp_path: Path,
    *,
    data_root: str | None = None,
    env_root: str | None = None,
    unset_env: bool = False,
) -> tuple[dict[str, object], str]:
    _, directory = scripts
    arguments = " -AsOfDate 2026-10-08"
    if data_root is not None:
        arguments += " -DataRoot " + _quote(data_root)
    command = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        f"& {_quote(directory / 'run_paper_daily.ps1')}{arguments}; "
        "$code = $LASTEXITCODE; "
        "[ordered]@{code=$code; root=$env:FINANCE_LAB_ROOT; utf8=$env:PYTHONUTF8; "
        'cwd=(Get-Location).Path} | ConvertTo-Json -Compress | ForEach-Object {"STATE:" + $_}'
    )
    result = _ps(command, tmp_path, env_root, unset_env=unset_env)
    states = [line[6:] for line in result.stdout.splitlines() if line.startswith("STATE:")]
    assert states, result.stdout + result.stderr
    return json.loads(states[-1]), result.stdout + result.stderr


@pytest.mark.parametrize("selection", ["explicit", "env"])
def test_runner_routes_real_cli_and_logs_to_selected_root(scripts, tmp_path, selection):
    project, _ = scripts
    selected = tmp_path / "数据 root"
    _marker(selected)
    other = tmp_path / "other"
    _marker(other)
    explicit = str(selected) if selection == "explicit" else None
    environment = str(other if selection == "explicit" else selected)
    state, output = _invoke_runner(scripts, tmp_path, data_root=explicit, env_root=environment)
    latest = selected / "outputs" / "default_paper_automation_latest.json"
    assert latest.is_file(), output
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["failed_step"] == "market_data_update"
    assert payload["error"]["type"] == "MarketDataStateError"
    logs = list((selected / "outputs" / "paper_automation_logs").glob("*.log"))
    assert len(logs) == 1, output
    assert "status=failed exit_code=1" in logs[0].read_text(encoding="utf-8-sig")
    assert state == {"code": 1, "root": environment, "utf8": "0", "cwd": str(tmp_path)}
    assert not (other / "outputs").exists()
    if selected != project:
        assert not (project / "outputs").exists()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_default_root_and_finally_at_interpreter_boundary(scripts, tmp_path, exit_code):
    project, directory = scripts
    runner = directory / "run_paper_daily.ps1"
    source = runner.read_text(encoding="utf-8-sig")
    start = source.index("    $arguments = @(")
    end = source.index("    $child = Start-Process", start)
    # Approved success-only native boundary: real Python prints observed env.
    # No paper-daily/source/network calls; main explicit/env tests use real CLI.
    probe = tmp_path / "native_probe.py"
    observed = tmp_path / "native_observed.txt"
    probe.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        + f'Path({str(observed)!r}).write_text(os.environ.get("FINANCE_LAB_ROOT", "absent"))\n'
        + ('sys.stderr.write("native boundary stderr\\n")\n' if exit_code else "")
        + f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    source = (
        source[:start]
        + "    $arguments = @('-B', "
        + _quote('"' + str(probe) + '"')
        + ")\n"
        + source[end:]
    )
    runner.write_text(source, encoding="utf-8-sig")
    assert "finance_lab.paper_process_worker" not in source
    state, output = _invoke_runner(scripts, tmp_path, unset_env=True)
    assert observed.read_text() == str(project), output
    assert state == {"code": exit_code, "root": None, "utf8": "0", "cwd": str(tmp_path)}
    logs = list((project / "outputs" / "paper_automation_logs").glob("*.log"))
    assert len(logs) == 1
    expected = "status=succeeded" if exit_code == 0 else "status=failed exit_code=7"
    assert expected in logs[0].read_text(encoding="utf-8-sig")


@pytest.mark.parametrize(
    "bad", ["missing", "file", "parent", "blank", "empty", "quote", "newline", "provider"]
)
def test_runner_rejects_invalid_roots_before_writes(scripts, tmp_path, bad):
    project, _ = scripts
    valid = tmp_path / "valid"
    _marker(valid)
    file_path = tmp_path / "file.txt"
    file_path.write_text("sentinel", encoding="utf-8")
    values = {
        "missing": str(tmp_path / "absent"),
        "file": str(file_path),
        "parent": str(valid / ".." / "valid"),
        "blank": "  ",
        "empty": "",
        "quote": str(valid) + '"',
        "newline": str(valid) + "\n",
        "provider": "Env:FINANCE_LAB_ROOT",
    }
    state, output = _invoke_runner(scripts, tmp_path, data_root=values[bad], env_root=str(valid))
    assert state["code"] != 0, output
    assert state["root"] == str(valid)
    assert state["utf8"] == "0"
    assert state["cwd"] == str(tmp_path)
    assert not (project / "outputs").exists()
    assert not (valid / "outputs").exists()
    assert not (tmp_path / "absent").exists()


def test_runner_rejects_junction_ancestor(scripts, tmp_path):
    target = tmp_path / "target"
    child = target / "child"
    child.mkdir(parents=True)
    link = tmp_path / "junction"
    result = _ps(
        f"New-Item -ItemType Junction -Path {_quote(link)} -Target {_quote(target)} | Out-Null",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    state, output = _invoke_runner(scripts, tmp_path, data_root=str(link / "child"))
    assert state["code"] != 0, output
    assert not list(child.iterdir())
    assert not (scripts[0] / "outputs").exists()


TASK_MOCKS = r"""
function New-ScheduledTaskAction {
    param($Execute,$Argument,$WorkingDirectory)
    $global:Captured=$Argument
    @{Execute=$Execute;Argument=$Argument;WorkingDirectory=$WorkingDirectory}
}
function New-ScheduledTaskTrigger { param([switch]$Weekly,$DaysOfWeek,$At) @{} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) @{} }
function New-ScheduledTaskSettingsSet {
    param([switch]$StartWhenAvailable,$MultipleInstances,$ExecutionTimeLimit)
    @{}
}
function Register-ScheduledTask {
    param($TaskName,$Action,$Trigger,$Principal,$Settings,$Description,[switch]$Force)
    $global:Registered=$true
}
function Get-ScheduledTask { param($TaskName,$ErrorAction) @{TaskName=$TaskName} }
function Unregister-ScheduledTask { param($TaskName,[switch]$Confirm) $global:Removed=$true }
"""


def _installer(scripts, tmp_path, arguments=""):
    command = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        + TASK_MOCKS
        + f"try {{ & {_quote(scripts[1] / 'install_paper_daily_task.ps1')} {arguments}; "
        '"ACTION:"+$global:Captured; "REGISTERED:"+$global:Registered; '
        '"REMOVED:"+$global:Removed } '
        "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 9 }"
    )
    return _ps(command, tmp_path)


def test_installer_quotes_absolute_root_and_retains_defaults(scripts, tmp_path):
    root = tmp_path / "中文 space"
    root.mkdir()
    result = _installer(scripts, tmp_path, "-DataRoot " + _quote(root))
    assert result.returncode == 0, result.stdout + result.stderr
    action = next(line[7:] for line in result.stdout.splitlines() if line.startswith("ACTION:"))
    assert f'-DataRoot "{root}"' in action
    assert "-StartDate 2018-01-01 -StaleAfterBusinessDays 3 -Portfolio default" in action
    assert "REGISTERED:True" in result.stdout
    default = _installer(scripts, tmp_path)
    assert default.returncode == 0, default.stderr
    assert "-DataRoot" not in default.stdout


@pytest.mark.parametrize(
    "path_case", ["plain", "trailing_backslash", "two_backslashes", "drive_root"]
)
def test_installer_action_roundtrips_through_real_native_command_line(scripts, tmp_path, path_case):
    root = tmp_path / "中文 space"
    root.mkdir()
    selected = {
        "plain": str(root),
        "trailing_backslash": str(root) + "\\",
        "two_backslashes": str(root) + "\\\\",
        "drive_root": root.anchor,
    }[path_case]
    # Receiver observes parameters only. No business CLI or task registration.
    receiver = scripts[1] / "run_paper_daily.ps1"
    receiver.write_text(
        "param($StartDate,$StaleAfterBusinessDays,$Portfolio,$DataRoot)\n"
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new()\n"
        "$DataRoot | ConvertTo-Json -Compress\n",
        encoding="utf-8-sig",
    )
    result = _installer(scripts, tmp_path, "-DataRoot " + _quote(selected))
    assert result.returncode == 0, result.stdout + result.stderr
    action = next(line[7:] for line in result.stdout.splitlines() if line.startswith("ACTION:"))
    env = os.environ.copy()
    env["FINANCE_LAB_ROOT"] = str(tmp_path / "process-safe-root")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.run(
        '"' + POWERSHELL + '" ' + action,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    observed = json.loads(child.stdout.strip())
    assert Path(observed) == Path(selected), {
        "action": action,
        "observed": observed,
        "expected": selected,
    }
    if path_case != "two_backslashes":  # Provider normalizes redundant separators.
        assert observed == selected


@pytest.mark.parametrize(
    "value", ["missing", "parent", "blank", "file", "quote", "newline", "provider"]
)
def test_installer_validates_before_task_api(scripts, tmp_path, value):
    file = tmp_path / "sentinel.txt"
    file.write_text("sentinel", encoding="utf-8")
    values = {
        "missing": str(tmp_path / "absent"),
        "parent": str(tmp_path / ".."),
        "blank": " ",
        "file": str(file),
        "quote": str(tmp_path) + '"',
        "newline": str(tmp_path) + "\n",
        "provider": "Env:PATH",
    }
    result = _installer(scripts, tmp_path, "-DataRoot " + _quote(values[value]))
    assert result.returncode != 0
    assert "REGISTERED:True" not in result.stdout
    assert not (tmp_path / "absent").exists()


def test_installer_uninstall_precedes_root_validation(scripts, tmp_path):
    result = _installer(scripts, tmp_path, "-Uninstall -DataRoot 'nonexistent'")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REMOVED:True" in result.stdout


def test_installer_rejects_junction_ancestor_before_task_api(scripts, tmp_path):
    target = tmp_path / "target"
    (target / "child").mkdir(parents=True)
    link = tmp_path / "junction"
    creation = _ps(
        f"New-Item -ItemType Junction -Path {_quote(link)} -Target {_quote(target)} | Out-Null",
        tmp_path,
    )
    assert creation.returncode == 0, creation.stderr
    result = _installer(scripts, tmp_path, "-DataRoot " + _quote(link / "child"))
    assert result.returncode != 0
    assert "REGISTERED:True" not in result.stdout


def test_runner_absolute_normalization_of_relative_root(scripts, tmp_path):
    selected = tmp_path / "relative 中文"
    _marker(selected)
    original = tmp_path / "original"
    _marker(original)
    state, output = _invoke_runner(
        scripts, tmp_path, data_root=selected.name, env_root=str(original)
    )
    assert state["code"] == 1, output
    assert state["root"] == str(original)
    assert (selected / "outputs" / "default_paper_automation_latest.json").is_file()
    assert not (scripts[0] / "outputs").exists()


def test_installer_retains_worktree_registration_guard(scripts, tmp_path):
    project = tmp_path / ".worktrees" / "temporary"
    directory = project / "scripts"
    directory.mkdir(parents=True)
    shutil.copyfile(
        scripts[1] / "install_paper_daily_task.ps1", directory / "install_paper_daily_task.ps1"
    )
    result = _installer((project, directory), tmp_path)
    assert result.returncode != 0
    assert "Refusing to register from .worktrees" in result.stderr
    assert "REGISTERED:True" not in result.stdout
