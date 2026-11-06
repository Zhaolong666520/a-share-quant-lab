from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

NOW = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)


def base() -> dict[str, Any]:
    attempt = {
        "run_id": "a" * 16,
        "status": "success",
        "started_at": "2026-10-14T10:30:00Z",
        "completed_at": "2026-10-14T10:31:00Z",
        "operation_status": "processed",
        "processed_dates": ["2026-10-14"],
        "run_identity": {"health": {"as_of_date": "2026-10-14"}},
    }
    return {
        "status": "ok",
        "command_exit_code": 0,
        "diagnostics": [],
        "automation": {"latest_attempt": attempt, "last_success": copy.deepcopy(attempt)},
        "ledger": {"last_valuation_date": "2026-10-14"},
        "market": {"publication_state": "completed"},
    }


def scheduler(**changes: Any) -> dict[str, Any]:
    result = {
        "schema_version": 1,
        "status": "available",
        "task_name": "Study Daily",
        "observed_at": NOW.isoformat(),
        "state": "Ready",
        "enabled": True,
        "last_run_time": "2026-10-14T10:30:00Z",
        "last_task_result": 0,
        "next_run_time": "2026-10-16T10:30:00Z",
        "missed_runs": 0,
        "start_when_available": True,
        "disallow_start_on_batteries": True,
        "stop_on_batteries": True,
        "wake_to_run": False,
        "trigger_origin": "unknown",
    }
    result.update(changes)
    return result


def log(root: Path, text: str) -> Path:
    directory = root / "outputs" / "paper_automation_logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "study-1-2026-10.log"
    path.write_text(text, encoding="utf-8-sig")
    return path


def run(root: Path, monkeypatch, *, evidence_base=None, task=None, expected="2026-10-15"):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    monkeypatch.setattr(
        module, "inspect_paper_operational_status", lambda *a, **kw: evidence_base or base()
    )
    monkeypatch.setattr(module, "read_scheduler_status", lambda *a: task or scheduler())
    return module.inspect_paper_runtime_status(
        root, "study-1", expected, now=NOW, task_name="Study Daily"
    )


def codes(result):
    return {item["code"] for item in result["diagnostics"]}


def test_new_start_cannot_be_hidden_by_old_success(tmp_path, monkeypatch):
    log(tmp_path, "[2026-10-15T19:09:48+08:00] status=started as_of=2026-10-15 portfolio=study-1\n")
    result = run(tmp_path, monkeypatch)
    assert result["status"] == "attention"
    assert result["application"]["unarchived_start"] == "observed"
    assert result["application"]["log"]["latest_start"]["as_of_date"] == "2026-10-15"
    assert result["progress"]["application"] == "behind"
    assert result["progress"]["valuation"] == "behind"
    assert "application_start_without_archive" in codes(result)
    assert result["application"]["last_success"]["run_id"] == "a" * 16
    assert result["safety"]["process_liveness_verified"] is False


def test_terminal_log_does_not_replace_archive_or_leak_error(tmp_path, monkeypatch):
    log(
        tmp_path,
        "[2026-10-15T19:09:48+08:00] status=started as_of=2026-10-15 portfolio=study-1\n"
        "[2026-10-15T19:10:00+08:00] status=failed exit_code=7 message=C:/secret/token\n",
    )
    result = run(tmp_path, monkeypatch)
    assert result["application"]["log"]["latest_terminal"]["exit_code"] == 7
    assert result["application"]["unarchived_start"] == "observed"
    assert "secret" not in json.dumps(result)


def test_explicit_expected_date_does_not_assume_today_is_due(tmp_path, monkeypatch):
    result = run(tmp_path, monkeypatch, expected="2026-10-14")
    assert result["progress"]["application"] == "met"
    assert result["progress"]["valuation"] == "met"
    assert "application_behind_expected_date" not in codes(result)
    assert result["scheduler"]["next_run_time"] == "2026-10-16T10:30:00Z"


@pytest.mark.parametrize("expected", ["2026-10-17", "20261015", "2026-02-30", ""])
def test_invalid_or_future_expected_date_is_error_without_system_query(
    tmp_path, monkeypatch, expected
):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    monkeypatch.setattr(module, "read_scheduler_status", lambda *a: pytest.fail("system queried"))
    result = module.inspect_paper_runtime_status(tmp_path, "study-1", expected, now=NOW)
    assert result["command_exit_code"] == 2


def test_old_schema_success_has_unknown_request_date(tmp_path, monkeypatch):
    state = base()
    state["automation"]["last_success"]["run_identity"] = None
    result = run(tmp_path, monkeypatch, evidence_base=state)
    assert result["progress"]["application"] == "unknown"
    assert "application_expected_date_unknown" in codes(result)


@pytest.mark.parametrize("status,code", [("Running", 267009), ("Queued", 267045)])
def test_in_progress_system_code_is_not_completed_failure(tmp_path, monkeypatch, status, code):
    result = run(tmp_path, monkeypatch, task=scheduler(state=status, last_task_result=code))
    assert result["scheduler"]["completion_state"] == "not_completed"
    assert "scheduler_last_run_failed" not in codes(result)
    assert result["command_exit_code"] == 1


def test_nonzero_completed_system_result_is_attention(tmp_path, monkeypatch):
    result = run(tmp_path, monkeypatch, task=scheduler(last_task_result=2147943467))
    assert result["scheduler"]["completion_state"] == "failed"
    assert "scheduler_last_run_failed" in codes(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unknown", "reason": "query_unavailable"},
        {"observed_at": "2026-10-14T00:00:00Z"},
        {"observed_at": "2026-10-17T00:00:00Z"},
        {"enabled": "true"},
        {"last_task_result": True},
        {"state": "invented"},
        {"last_run_time": "2026-10-17T00:00:00Z"},
        {"task_name": "Another task"},
    ],
)
def test_unavailable_invalid_stale_scheduler_preserves_application(tmp_path, monkeypatch, changes):
    result = run(tmp_path, monkeypatch, task=scheduler(**changes))
    assert result["scheduler"]["status"] == "unknown"
    assert result["application"]["last_success"]["run_id"] == "a" * 16
    assert result["status"] == "attention"


def test_market_incomplete_never_asserts_running_process(tmp_path, monkeypatch):
    from tests.test_paper_operational_status import market

    market(
        tmp_path,
        status="updating",
        phase="fetch",
        end="2026-10-15",
        started_at="2026-10-15T11:09:52+00:00",
        completed_at=None,
        summary_sha256=None,
    )
    result = run(tmp_path, monkeypatch)
    assert result["market_update"]["status"] == "updating"
    assert result["market_update"]["elapsed_seconds"] > 60000
    assert result["market_update"]["process_state"] == "unknown"
    assert "market_update_incomplete" in codes(result)


@pytest.mark.parametrize("variant", ["bad_date", "wrong_portfolio", "large", "bad_utf8"])
def test_invalid_log_is_unknown_without_discarding_archive(tmp_path, monkeypatch, variant):
    path = log(tmp_path, "[broken] status=started as_of=2026-10-15 portfolio=study-1\n")
    if variant == "wrong_portfolio":
        path.write_text("[2026-10-15T11:00:00Z] status=started as_of=2026-10-15 portfolio=other\n")
    elif variant == "large":
        path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    elif variant == "bad_utf8":
        path.write_bytes(b"\xff")
    result = run(tmp_path, monkeypatch)
    assert result["application"]["log"]["state"] == "unavailable"
    assert result["application"]["unarchived_start"] == "unknown"


def test_future_log_and_marker_are_unknown(tmp_path, monkeypatch):
    from tests.test_paper_operational_status import market

    log(tmp_path, "[2026-10-17T10:00:00Z] status=started as_of=2026-10-17 portfolio=study-1\n")
    market(
        tmp_path,
        status="updating",
        phase="fetch",
        started_at="2026-10-17T10:00:00Z",
        completed_at=None,
        summary_sha256=None,
    )
    result = run(tmp_path, monkeypatch)
    assert result["application"]["unarchived_start"] == "unknown"
    assert result["market_update"]["state"] == "unavailable"
    assert "runtime_evidence_future" in codes(result)


def test_capture_change_clears_runtime_evidence(tmp_path, monkeypatch):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    path = log(
        tmp_path, "[2026-10-15T11:00:00Z] status=started as_of=2026-10-15 portfolio=study-1\n"
    )

    def mutate(*args, **kwargs):
        path.write_text("[2026-10-16T01:00:00Z] status=succeeded\n")
        return base()

    monkeypatch.setattr(module, "inspect_paper_operational_status", mutate)
    monkeypatch.setattr(module, "read_scheduler_status", lambda *a: scheduler())
    result = module.inspect_paper_runtime_status(tmp_path, "study-1", "2026-10-15", now=NOW)
    assert result["application"]["log"]["state"] == "changed"
    assert result["application"]["unarchived_start"] == "unknown"


def test_process_read_change_is_included_in_final_consistency_check(tmp_path, monkeypatch):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    path = log(tmp_path,
               "[2026-10-15T11:00:00Z] status=started as_of=2026-10-15 portfolio=study-1\n")

    def mutate(*args, **kwargs):
        path.write_text("[2026-10-16T01:00:00Z] status=succeeded\n")
        return {"state": "missing"}

    monkeypatch.setattr(module, "inspect_process_evidence", mutate)
    result = run(tmp_path, monkeypatch)
    assert result["observation"]["runtime_input_changed"] is True
    assert result["application"]["log"]["state"] == "changed"


def test_real_cli_strict_read_only_empty_root_and_exit_codes(tmp_path):
    from tests.test_paper_operational_status import fingerprint

    before = fingerprint(tmp_path)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    command = [
        sys.executable,
        "-B",
        "-m",
        "finance_lab.paper_runtime_status",
        "--root",
        str(tmp_path),
        "--portfolio",
        "study-1",
        "--no-scheduler",
    ]
    checked = subprocess.run(
        command + ["--expected-as-of", "2026-10-14"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert checked.returncode == 1, checked.stderr
    result = json.loads(checked.stdout)
    assert result["scheduler"]["status"] == "unknown"
    assert result["safety"]["current_health_verified"] is False
    assert result["safety"]["scheduler_application_correlation"] == "not_verified"
    assert fingerprint(tmp_path) == before
    bad = subprocess.run(
        command + ["--expected-as-of", "bad"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert bad.returncode == 2
    assert json.loads(bad.stdout)["status"] == "error"


def test_failed_terminal_after_archive_is_not_green(tmp_path, monkeypatch):
    log(
        tmp_path,
        "[2026-10-14T10:29:58Z] status=started as_of=2026-10-14 portfolio=study-1\n"
        "[2026-10-15T10:32:00Z] status=failed exit_code=1 message=private\n",
    )
    result = run(tmp_path, monkeypatch, expected="2026-10-14")
    assert "application_log_failure_newer_than_archive" in codes(result)


def test_read_only_with_real_valid_archive_and_ledger(tmp_path, monkeypatch):
    from tests.test_paper_automation_history import archive, payload
    from tests.test_paper_operational_status import fingerprint, ledger, market

    module = importlib.import_module("finance_lab.paper_runtime_status")
    archive(tmp_path, payload())
    market(tmp_path)
    ledger(tmp_path)
    log(
        tmp_path,
        "[2026-09-08T10:29:59Z] status=started as_of=2026-09-08 portfolio=study-1\n"
        "[2026-09-08T10:31:01Z] status=succeeded\n",
    )
    monkeypatch.setattr(module, "read_scheduler_status", lambda *a: scheduler())
    before = fingerprint(tmp_path)
    result = module.inspect_paper_runtime_status(tmp_path, "study-1", "2026-09-08", now=NOW)
    assert result["application"]["unarchived_start"] == "not_observed"
    assert result["recorded_status"]["ledger"]["audit_status"] == "passed"
    assert fingerprint(tmp_path) == before


def test_current_evidence_without_report_binding_keeps_attention(tmp_path, monkeypatch):
    from tests.test_paper_operational_status import market

    market(tmp_path)
    log(
        tmp_path,
        "[2026-10-14T10:29:59Z] status=started as_of=2026-10-14 portfolio=study-1\n"
        "[2026-10-14T10:31:00Z] status=succeeded\n",
    )
    result = run(
        tmp_path, monkeypatch, expected="2026-10-14", task=scheduler(trigger_origin="manual")
    )
    assert result["command_exit_code"] == 1
    assert "report_json_not_verified" in codes(result)
    assert result["safety"]["natural_run_acceptance_verified"] is False


@pytest.mark.parametrize("failure", ["denied", "timeout", "invalid", "nonzero", "large"])
def test_scheduler_process_failure_stays_unknown(monkeypatch, failure):
    module = importlib.import_module("finance_lab.paper_runtime_evidence")
    monkeypatch.setattr(module, "_WINDOWS", True)
    monkeypatch.setattr(module.shutil, "which", lambda *a: "powershell.exe")

    def boundary(args):
        assert "-File" in args
        if failure == "denied":
            raise PermissionError("secret path")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 15)
        output = b"secret invalid json" if failure == "invalid" else b"{}"
        if failure == "large":
            output = b"x" * 65537
        return None if failure == "nonzero" else output

    monkeypatch.setattr(module, "_bounded_query", boundary)
    result = module.read_scheduler_status("Study Daily")
    assert result["status"] == "unknown"
    assert "secret" not in json.dumps(result)


def test_powershell_resource_real_query_failure_protocol(tmp_path):
    import shutil

    from tests.test_paper_script_data_root import _ps, _quote

    if shutil.which("powershell.exe") is None:
        pytest.skip("Windows PowerShell required")
    script = (
        Path(__file__).resolve().parents[1] / "src/finance_lab/resources/paper_scheduler_status.ps1"
    )
    assert script.is_file()
    result = _ps(
        "function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) "
        "throw 'secret CIM access denied' }; & " + _quote(script),
        tmp_path,
    )
    value = json.loads(result.stdout)
    assert value["status"] == "unknown"
    assert "secret" not in result.stdout


def test_import_does_not_load_writing_pipeline():
    check = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import finance_lab.paper_runtime_status; import sys,json; "
            "print(json.dumps(sorted(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert check.returncode == 0
    loaded = json.loads(check.stdout)
    for name in (
        "finance_lab.cli",
        "finance_lab.paper_pipeline",
        "finance_lab.paper_automation",
        "finance_lab.paper_report",
        "matplotlib",
    ):
        assert name not in loaded


def test_future_archive_cannot_satisfy_expected_date(tmp_path, monkeypatch):
    state = base()
    for key in ("latest_attempt", "last_success"):
        state["automation"][key]["started_at"] = "2026-10-17T10:30:00Z"
        state["automation"][key]["completed_at"] = "2026-10-17T10:31:00Z"
    result = run(tmp_path, monkeypatch, evidence_base=state, expected="2026-10-14")
    assert result["progress"]["application"] == "unknown"
    assert "archive_timestamp_future" in codes(result)


def test_old_system_success_cannot_satisfy_expected_run_date(tmp_path, monkeypatch):
    result = run(tmp_path, monkeypatch)
    assert "scheduler_behind_expected_date" in codes(result)


def test_log_file_limit_is_unknown_not_a_truncated_success(tmp_path, monkeypatch):
    path = log(tmp_path, "[2026-10-14T10:31:00Z] status=succeeded\n")
    for year in range(2010, 2021):
        for month in range(1, 13):
            (path.parent / f"study-1-{year}-{month:02}.log").write_text("")
    result = run(tmp_path, monkeypatch)
    assert result["application"]["log"]["state"] == "unavailable"


@pytest.mark.parametrize(
    "event_id,origin", [(107, "time_trigger"), (110, "manual"), (None, "unknown")]
)
def test_powershell_query_snapshot_uses_only_available_trigger_event(tmp_path, event_id, origin):
    import shutil

    from tests.test_paper_script_data_root import _ps, _quote

    if shutil.which("powershell.exe") is None:
        pytest.skip("Windows PowerShell required")
    script = Path(__file__).resolve().parents[1] / (
        "src/finance_lab/resources/paper_scheduler_status.ps1"
    )
    event = (
        "function Get-WinEvent { param($FilterHashtable,$MaxEvents,$ErrorAction) "
        "throw 'event log unavailable' }"
        if event_id is None
        else "function Get-WinEvent { param($FilterHashtable,$MaxEvents,$ErrorAction) "
        f"$e=[pscustomobject]@{{Id={event_id}}}; "
        "$e | Add-Member -MemberType ScriptMethod -Name ToXml -Value { "
        '\'<Event><EventData><Data Name="TaskName">\\Study Daily</Data>'
        "</EventData></Event>' }; $e }"
    )
    command = (
        "function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) "
        "[pscustomobject]@{State='Ready';Settings=[pscustomobject]@{Enabled=$true; "
        "StartWhenAvailable=$true;DisallowStartIfOnBatteries=$true; "
        "StopIfGoingOnBatteries=$true;WakeToRun=$false}} }; "
        "function Get-ScheduledTaskInfo { param($InputObject,$ErrorAction) "
        "[pscustomobject]@{LastRunTime=[datetime]'2026-10-14T18:30:00';LastTaskResult=0; "
        "NextRunTime=[datetime]'2026-10-15T18:30:00';NumberOfMissedRuns=0} }; "
        + event
        + "; & "
        + _quote(script)
        + " -TaskName 'Study Daily'"
    )
    result = _ps(command, tmp_path)
    payload = json.loads(result.stdout)
    assert payload["status"] == "available", result.stderr
    assert payload["trigger_origin"] == origin
    assert payload["start_when_available"] is True
    assert payload["last_task_result"] == 0


@pytest.mark.parametrize("seconds", [0.000001, 1, 5])
def test_small_future_snapshot_never_participates_in_ok(tmp_path, monkeypatch, seconds):
    from tests.test_paper_operational_status import market

    market(tmp_path)
    log(
        tmp_path,
        "[2026-10-14T10:29:59Z] status=started as_of=2026-10-14 portfolio=study-1\n"
        "[2026-10-14T10:31:00Z] status=succeeded\n",
    )
    result = run(
        tmp_path,
        monkeypatch,
        expected="2026-10-14",
        task=scheduler(
            trigger_origin="manual", observed_at=(NOW + timedelta(seconds=seconds)).isoformat()
        ),
    )
    assert result["scheduler"]["status"] == "unknown"
    assert result["command_exit_code"] == 1
    assert result["application"]["last_success"] is not None


def test_future_request_date_alone_invalidates_log_evidence(tmp_path, monkeypatch):
    from tests.test_paper_operational_status import market

    market(tmp_path)
    log(
        tmp_path,
        "[2026-10-14T10:29:59Z] status=started as_of=2099-01-01 portfolio=study-1\n"
        "[2026-10-14T10:31:00Z] status=succeeded\n",
    )
    result = run(
        tmp_path, monkeypatch, expected="2026-10-14", task=scheduler(trigger_origin="manual")
    )
    assert result["application"]["log"]["state"] == "unavailable"
    assert result["application"]["unarchived_start"] == "unknown"
    assert result["application"]["last_success"] is not None
    assert "runtime_evidence_future" in codes(result)
    assert result["command_exit_code"] == 1


@pytest.mark.parametrize(
    "code", [0x41300, 0x41302, 0x41303, 0x41304, 0x41305, 0x41307, 0x41308, 0x4131B, 0x4131C]
)
def test_noncompletion_scheduler_status_is_not_action_failure(tmp_path, monkeypatch, code):
    result = run(tmp_path, monkeypatch, task=scheduler(last_task_result=code))
    assert result["scheduler"]["completion_state"] == "unknown"
    assert result["scheduler"]["last_task_result"] == code
    assert "scheduler_last_run_failed" not in codes(result)
    assert result["command_exit_code"] == 1


@pytest.mark.parametrize("code", [0x41306, 3221225786, 7])
def test_terminated_or_nonzero_action_still_reports_failure(tmp_path, monkeypatch, code):
    result = run(tmp_path, monkeypatch, task=scheduler(last_task_result=code))
    assert result["scheduler"]["completion_state"] == "failed"
    assert "scheduler_last_run_failed" in codes(result)


def test_other_valid_portfolio_logs_do_not_corrupt_or_exhaust_target(tmp_path, monkeypatch):
    path = log(
        tmp_path, "[2026-10-14T10:29:59Z] status=started as_of=2026-10-14 portfolio=study-1\n"
    )
    for year in range(2010, 2021):
        for month in range(1, 13):
            (path.parent / f"study-1-extra-{year}-{month:02}.log").write_bytes(b"\xff")
    result = run(tmp_path, monkeypatch)
    assert result["application"]["log"]["state"] == "available"
    assert result["application"]["log"]["latest_start"]["as_of_date"] == "2026-10-14"


@pytest.mark.parametrize("owner,expected", [("study-1-extra", "available"), ("study-1", "changed")])
def test_log_membership_changes_only_for_target_portfolio(tmp_path, monkeypatch, owner, expected):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    path = log(
        tmp_path, "[2026-10-14T10:29:59Z] status=started as_of=2026-10-14 portfolio=study-1\n"
    )

    def mutate(*args, **kwargs):
        (path.parent / f"{owner}-2026-09.log").write_text("")
        return base()

    monkeypatch.setattr(module, "inspect_paper_operational_status", mutate)
    monkeypatch.setattr(module, "read_scheduler_status", lambda *a: scheduler())
    result = module.inspect_paper_runtime_status(tmp_path, "study-1", "2026-10-14", now=NOW)
    assert result["application"]["log"]["state"] == expected
    assert result["observation"]["runtime_input_changed"] is (expected == "changed")


@pytest.mark.parametrize("variant", ["older_conflict", "latest_conflict", "identical_duplicates"])
def test_log_latest_selection_preserves_tie_semantics(tmp_path, monkeypatch, variant):
    path = log(tmp_path, "[2026-10-14T10:31:00Z] status=succeeded\n")
    old = path.parent / "study-1-2026-09.log"
    if variant == "older_conflict":
        old.write_text(
            "[2026-09-01T10:31:00Z] status=succeeded\n"
            "[2026-09-01T10:31:00Z] status=failed exit_code=1 message=private\n"
        )
    elif variant == "latest_conflict":
        old.write_text("[2026-10-14T10:31:00Z] status=failed exit_code=1 message=private\n")
    else:
        old.write_text("[2026-10-14T10:31:00Z] status=succeeded\n" * 100)
    result = run(tmp_path, monkeypatch)
    observed = result["application"]["log"]
    assert observed["state"] == ("unavailable" if variant == "latest_conflict" else "available")
    if variant != "latest_conflict":
        assert observed["latest_terminal"]["status"] == "succeeded"


@pytest.mark.parametrize("mode", ["overflow", "timeout", "stderr", "nonzero"])
def test_real_query_pipe_boundary_is_bounded_and_reaps_child(monkeypatch, mode):
    import time

    module = importlib.import_module("finance_lab.paper_runtime_evidence")
    monkeypatch.setattr(module, "_QUERY_TIMEOUT", 10 if mode == "overflow" else 1)
    program = {
        "overflow": (
            "import sys,time; sys.stdout.buffer.write(b'x'*131072); "
            "sys.stdout.flush(); time.sleep(30)"
        ),
        "timeout": "import time; time.sleep(30)",
        "stderr": "import sys; sys.stderr.buffer.write(b'x'*1048576); print('{}')",
        "nonzero": "import sys; print('{}'); sys.exit(7)",
    }[mode]
    real_popen = subprocess.Popen
    children = []

    def owned(*args, **kwargs):
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs.get("shell", False) is False
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", owned)
    begun = time.monotonic()
    output = module._bounded_query([sys.executable, "-B", "-c", program])
    assert time.monotonic() - begun < 5
    if mode == "stderr":
        assert output == (b"{}\r\n" if os.name == "nt" else b"{}\n")
    else:
        assert output is None
    assert children and all(child.poll() is not None for child in children)


@pytest.mark.parametrize("size", [65536, 65537])
def test_real_query_exact_stdout_limit(size):
    module = importlib.import_module("finance_lab.paper_runtime_evidence")
    output = module._bounded_query(
        [sys.executable, "-B", "-c", f"import sys; sys.stdout.buffer.write(b'x'*{size})"]
    )
    assert output == (b"x" * size if size == 65536 else None)


def test_timer_start_failure_reaps_owned_query_without_joining_unstarted_thread(monkeypatch):
    import time

    module = importlib.import_module("finance_lab.paper_runtime_evidence")
    real_popen = subprocess.Popen
    children = []

    def owned(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    class BrokenTimer:
        def __init__(self, *args):
            self.daemon = False

        def start(self):
            raise RuntimeError("cannot start new thread: private details")

        def cancel(self):
            pass

        def join(self):
            pytest.fail("joined an unstarted timer")

    monkeypatch.setattr(module.subprocess, "Popen", owned)
    monkeypatch.setattr(module, "Timer", BrokenTimer)
    begun = time.monotonic()
    output = module._bounded_query([sys.executable, "-B", "-c", "import time; time.sleep(2)"])
    assert output is None
    assert time.monotonic() - begun < 5
    assert children and all(child.poll() not in (None, 0) for child in children)
