"""Synthetic integration fixtures for an archived quarantine rejection, not market data."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_paper_automation_history import archive, payload
from tests.test_paper_operational_status import fingerprint, ledger, market
from tests.test_paper_runtime_status import log, scheduler

OBSERVED = datetime(2026, 10, 16, 10, 32, tzinfo=UTC)


@pytest.fixture
def rejected_root(tmp_path: Path) -> Path:
    """Build local synthetic evidence before measuring read-only diagnosis."""
    archive(
        tmp_path,
        payload(
            schema_version=2,
            attempt_id="a" * 32,
            as_of_date="2026-10-14",
            started_at="2026-10-14T10:30:00Z",
            completed_at="2026-10-14T10:31:00Z",
            operation_status="no-op",
            processed_dates=[],
            ledger_committed=False,
            paper_report_path=None,
            run_identity={
                "market": {"batch_id": "a" * 32, "summary_sha256": "c" * 64},
                "health": {"dataset_id": "synthetic-old-health", "as_of_date": "2026-10-14"},
                "ledger": {"scope": "complete", "runs": []},
                "report": None,
            },
        ),
    )
    archive(
        tmp_path,
        payload(
            schema_version=2,
            attempt_id="b" * 32,
            as_of_date="2026-10-16",
            started_at="2026-10-16T10:30:04.685958Z",
            completed_at="2026-10-16T10:30:04.687612Z",
            status="failed",
            steps=[{"name": "market_data_update", "status": "failed"}],
            failed_step="market_data_update",
            error={
                "type": "MarketDataStateError",
                "code": "market_data_update_failed",
                "message": "private-synthetic-error",
            },
            successful_instruments=None,
            failed_sources=None,
            market_data_committed=None,
            data_health_status=None,
            data_health_report_path=None,
            operation_status=None,
            processed_dates=[],
            ledger_committed=None,
            next_action="private-synthetic-next-action",
            paper_report_path=None,
            run_identity={
                "market": None,
                "health": None,
                "ledger": {"scope": "unknown", "runs": []},
                "report": None,
            },
        ),
    )
    market(
        tmp_path,
        status="updating",
        end="2026-10-15",
        started_at="2026-10-15T10:30:00Z",
        phase="fetch",
        completed_at=None,
        summary_sha256=None,
    )
    ledger(tmp_path)
    log(
        tmp_path,
        "[2026-10-14T10:29:59Z] status=started as_of=2026-10-14 portfolio=study-1\n"
        "[2026-10-14T10:31:01Z] status=succeeded\n"
        "[2026-10-16T10:30:02.178903+00:00] status=started as_of=2026-10-16 portfolio=study-1\n"
        "[2026-10-16T10:30:04.931433+00:00] status=failed exit_code=1 "
        "message=private-synthetic-log\n",
    )
    return tmp_path


def assert_rejection(result: dict) -> None:
    assert result["status"] == "attention"
    assert result["command_exit_code"] == 1
    latest = result["application"]["latest_archived_attempt"]
    success = result["application"]["last_success"]
    assert latest["attempt_id"] == "b" * 32
    assert latest["status"] == "failed"
    assert latest["failed_step"] == "market_data_update"
    assert latest["error_code"] == "market_data_update_failed"
    assert latest["market_data_committed"] is None
    assert latest["ledger_committed"] is None
    assert latest["processed_dates"] == []
    assert success["attempt_id"] == "a" * 32
    assert success["status"] == "success"
    assert result["application"]["unarchived_start"] == "not_observed"
    assert result["progress"]["last_success_as_of"] == "2026-10-14"
    assert result["progress"]["application"] == "behind"
    assert result["progress"]["valuation"] == "behind"
    recorded = result["recorded_status"]
    assert recorded["market"]["isolated"] is True
    assert recorded["ledger"]["audit_status"] == "passed"
    assert result["market_update"]["status"] == "updating"
    assert result["market_update"]["process_state"] == "unknown"
    assert result["safety"]["current_health_verified"] is False
    assert result["safety"]["scheduler_application_correlation"] == "not_verified"
    assert result["safety"]["natural_run_acceptance_verified"] is False
    assert "private-synthetic" not in json.dumps(result)


@pytest.mark.parametrize("system_result", [0, 1])
def test_archived_rejection_survives_system_result(rejected_root, monkeypatch, system_result):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    monkeypatch.setattr(
        module,
        "read_scheduler_status",
        lambda *_: scheduler(
            observed_at=OBSERVED.isoformat(),
            last_run_time="2026-10-16T10:30:00Z",
            next_run_time="2026-10-19T10:30:00Z",
            last_task_result=system_result,
            trigger_origin="time_trigger",
        ),
    )
    before = fingerprint(rejected_root)
    result = module.inspect_paper_runtime_status(
        rejected_root, "study-1", "2026-10-16", now=OBSERVED, task_name="Study Daily"
    )
    assert_rejection(result)
    assert result["scheduler"]["completion_state"] == (
        "succeeded" if system_result == 0 else "failed"
    )
    assert fingerprint(rejected_root) == before


def test_archived_rejection_real_cli_stays_read_only(rejected_root):
    before = fingerprint(rejected_root)
    checked = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "finance_lab.paper_runtime_status",
            "--root",
            str(rejected_root),
            "--portfolio",
            "study-1",
            "--expected-as-of",
            "2026-10-16",
            "--no-scheduler",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=30,
    )
    assert checked.returncode == 1, checked.stderr
    result = json.loads(checked.stdout)
    assert_rejection(result)
    assert result["scheduler"]["status"] == "unknown"
    assert fingerprint(rejected_root) == before
