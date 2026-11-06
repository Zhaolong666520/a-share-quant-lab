"""Synthetic scheduler observations only; never query the installed task."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.test_paper_script_data_root import _ps, _quote


@pytest.mark.parametrize(
    "change",
    ["none", "last_run", "result", "state", "action", "setting",
     "task_unavailable", "info_unavailable"],
)
def test_scheduler_query_requires_stable_observations(tmp_path: Path, change: str) -> None:
    if shutil.which("powershell.exe") is None:
        pytest.skip("Windows PowerShell required")
    script = Path(__file__).resolve().parents[1] / (
        "src/finance_lab/resources/paper_scheduler_status.ps1"
    )
    command = (
        f"$global:queryTestVariant='{change}';$global:queryTestReads=0;"
        "$global:queryTestLast=[datetime]'2026-10-19T18:30:00';"
        "function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) "
        "$global:queryTestReads++;$state='Ready';$argument='-File private-original.ps1';"
        "if($global:queryTestReads -gt 1 -and $global:queryTestVariant -eq 'task_unavailable')"
        "{throw 'private-query-error'};$enabled=$true;"
        "if($global:queryTestReads -gt 1 -and $global:queryTestVariant -eq 'setting')"
        "{$enabled=$false};"
        "if($global:queryTestReads -gt 1 -and $global:queryTestVariant -eq 'state')"
        "{$state='Running'};"
        "if($global:queryTestReads -gt 1 -and $global:queryTestVariant -eq 'action')"
        "{$argument='private-changed'};"
        "[pscustomobject]@{State=$state;Actions=@([pscustomobject]@{Execute='powershell.exe';"
        "Arguments=$argument;WorkingDirectory='D:\\synthetic'});Settings=[pscustomobject]@{"
        "Enabled=$enabled;StartWhenAvailable=$true;DisallowStartIfOnBatteries=$true;"
        "StopIfGoingOnBatteries=$true;WakeToRun=$false}} };"
        "function Get-ScheduledTaskInfo { param($InputObject,$ErrorAction) "
        "if($global:queryTestReads -gt 1 -and $global:queryTestVariant -eq 'info_unavailable')"
        "{throw 'private-query-error'};"
        "$last=$global:queryTestLast;$result=0;"
        "if($global:queryTestReads -gt 1 -and $global:queryTestVariant -eq 'last_run')"
        "{$last=$last.AddMinutes(1)};"
        "if($global:queryTestReads -gt 1 -and $global:queryTestVariant -eq 'result'){$result=7};"
        "[pscustomobject]@{LastRunTime=$last;LastTaskResult=$result;"
        "NextRunTime=$global:queryTestLast.AddDays(1);NumberOfMissedRuns=0} };"
        "function Get-WinEvent { param($FilterHashtable,$MaxEvents,$ErrorAction) "
        "throw 'synthetic unavailable event log' };"
        "& " + _quote(script) + " -TaskName 'Synthetic Daily'"
    )
    process = _ps(command, tmp_path)
    assert process.returncode == 0, process.stderr
    payload = json.loads(process.stdout)
    assert payload["status"] == ("available" if change == "none" else "unknown")
    if change != "none":
        assert payload["reason"] == (
            "query_unavailable" if change.endswith("unavailable") else "query_changed"
        )
    assert "private-" not in process.stdout
