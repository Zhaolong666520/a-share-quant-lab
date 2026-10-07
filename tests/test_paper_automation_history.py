from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


def payload(**changes: Any) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "workflow": "paper_daily",
        "status": "success",
        "portfolio_id": "study-1",
        "start_date": "2020-01-01",
        "as_of_date": "2026-09-08",
        "stale_after_business_days": 3,
        "started_at": "2026-09-08T10:30:00Z",
        "completed_at": "2026-09-08T10:31:00Z",
        "steps": [
            {"name": n, "status": "succeeded"}
            for n in ("market_data_update", "data_health", "paper_run")
        ],
        "failed_step": None,
        "error": None,
        "successful_instruments": 1,
        "failed_sources": 0,
        "market_data_committed": True,
        "data_health_status": "pass",
        "data_health_report_path": "private/health.html",
        "operation_status": "processed",
        "processed_dates": ["2026-09-08"],
        "ledger_committed": True,
        "next_action": None,
        "paper_report_path": "C:/private/report.html",
        "broker_connected": False,
        "authorizes_real_money": False,
    }
    value.update(changes)
    return value


def seal(value: dict[str, Any]) -> dict[str, Any]:
    value = {k: v for k, v in value.items() if k != "run_id"}
    digest = hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
    )
    return {**value, "run_id": digest.hexdigest()[:16]}


def archive(root: Path, value: dict[str, Any], *, mirror: bool = True) -> Path:
    value = seal(value)
    directory = root / "outputs" / "paper_automation_runs"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(value["started_at"].replace("Z", "+00:00"))
    name = f"study-1_{stamp.strftime('%Y%m%dT%H%M%S%fZ')}_{value['run_id']}.json"
    target = directory / name
    target.write_text(json.dumps(value), encoding="utf-8")
    if mirror:
        (root / "outputs" / "study-1_paper_automation_latest.json").write_text(
            json.dumps(value), encoding="utf-8"
        )
    return target


def inspect(root: Path) -> dict[str, Any]:
    from finance_lab.paper_automation_history import inspect_automation_history

    return inspect_automation_history(root, "study-1")


def test_success_and_legacy_unknown_fields(tmp_path: Path) -> None:
    archive(tmp_path, payload())
    result = inspect(tmp_path)
    assert result["state"] == "available"
    assert result["latest_attempt"]["exit_code"] == 0
    assert result["latest_attempt"]["exit_code_origin"] == "derived"
    assert result["latest_mirror"]["state"] == "matched"
    old = payload(started_at="2026-09-09T10:30:00Z", completed_at="2026-09-09T10:31:00Z")
    for key in ("failed_sources", "ledger_committed", "market_data_committed", "next_action"):
        old.pop(key)
    archive(tmp_path, old)
    result = inspect(tmp_path)
    assert result["latest_attempt"]["ledger_committed"] is None
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"workflow": "other"},
        {"portfolio_id": "other"},
        {"broker_connected": 0},
        {"authorizes_real_money": True},
        {"successful_instruments": True},
        {"failed_sources": -1},
        {"unexpected": 1},
        {"started_at": "2026-09-08T10:30:00"},
        {"as_of_date": "2026-02-30"},
        {"completed_at": "2026-09-08T10:00:00Z"},
        {"ledger_committed": 1},
        {"processed_dates": []},
        {"operation_status": "status"},
        {"processed_dates": ["2026-09-08", "2026-09-07"]},
        {"steps": [{"name": "paper_run", "status": "succeeded"}]},
        {"failed_step": "paper_run"},
        {"data_health_status": "error"},
    ],
)
def test_semantic_invalid_even_with_valid_hash(tmp_path: Path, changes: dict[str, Any]) -> None:
    value = payload(**changes)
    if "started_at" in changes:
        # A parseable but noncanonical timestamp still has a plausible filename.
        target = archive(tmp_path, payload())
        target.write_text(json.dumps(seal(value)), encoding="utf-8")
    else:
        archive(tmp_path, value)
    assert inspect(tmp_path)["latest_attempt"] is None


def test_new_failed_keeps_old_success_and_commit_facts(tmp_path: Path) -> None:
    archive(tmp_path, payload())
    failed = payload(
        started_at="2026-09-09T10:30:00Z",
        completed_at="2026-09-09T10:31:00Z",
        status="failed",
        failed_step="paper_report",
        paper_report_path=None,
        error={"type": "OSError", "code": "paper_report_failed", "message": "secret"},
        next_action="secret",
    )
    failed["steps"].append({"name": "paper_report", "status": "failed"})
    archive(tmp_path, failed)
    result = inspect(tmp_path)
    assert result["latest_attempt"]["state"] == "failed_after_commit"
    assert result["last_success"]["status"] == "success"
    assert result["latest_attempt"]["processed_dates"] == ["2026-09-08"]
    assert "secret" not in json.dumps(result)


def test_bad_newer_and_unsortable_archive_never_fall_back(tmp_path: Path) -> None:
    archive(tmp_path, payload())
    new = archive(
        tmp_path, payload(started_at="2026-09-09T10:30:00Z", completed_at="2026-09-09T10:31:00Z")
    )
    new.write_text('{"broken": true}', encoding="utf-8")
    result = inspect(tmp_path)
    assert result["latest_attempt"] is None
    assert result["last_success"] is not None
    assert result["scan"]["complete"] is False
    new.unlink()
    (new.parent / "study-1_bad.json").write_text("{}", encoding="utf-8")
    assert inspect(tmp_path)["state"] == "unknown"


def test_same_start_is_ambiguous_and_mirror_cannot_decide(tmp_path: Path) -> None:
    archive(tmp_path, payload())
    archive(tmp_path, payload(data_health_status="warning"))
    result = inspect(tmp_path)
    assert result["state"] == "ambiguous"
    assert result["latest_attempt"] is None
    assert len(result["latest_candidates"]) == 2
    assert result["last_success"] is None


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', "[]"])
def test_strict_json_archive(tmp_path: Path, content: str) -> None:
    target = archive(tmp_path, payload())
    target.write_text(content, encoding="utf-8")
    assert inspect(tmp_path)["latest_attempt"] is None


def test_current_writer_compatibility(tmp_path: Path) -> None:
    from datetime import date

    from finance_lab.paper_automation import PaperAutomationActions, run_paper_daily_automation
    from finance_lab.paper_models import PaperOperationResult

    moments = iter(
        [datetime(2026, 9, 8, 10, 30, tzinfo=UTC), datetime(2026, 9, 8, 10, 31, tzinfo=UTC)]
    )
    operation = PaperOperationResult("no-op", "study-1", (), (), None, Path("report.html"))
    run_paper_daily_automation(
        portfolio_id="study-1",
        root=tmp_path,
        as_of_date=date(2026, 9, 8),
        actions=PaperAutomationActions(
            lambda **kw: {"status": "success", "successful_instruments": 1, "failed_sources": 0},
            lambda **kw: (Path("health.html"), {"health_status": "pass"}),
            lambda **kw: operation,
            lambda **kw: operation,
        ),
        clock=lambda: next(moments),
    )
    result = inspect(tmp_path)
    assert result["latest_attempt"]["operation_status"] == "no-op"
    assert result["latest_attempt"]["ledger_committed"] is False


@pytest.mark.parametrize("mode", ["behind", "invalid", "orphan", "missing"])
def test_mirror_states_do_not_replace_archive(tmp_path: Path, mode: str) -> None:
    archive(tmp_path, payload())
    mirror = tmp_path / "outputs" / "study-1_paper_automation_latest.json"
    archive(
        tmp_path,
        payload(started_at="2026-09-09T10:30:00Z", completed_at="2026-09-09T10:31:00Z"),
        mirror=False,
    )
    if mode == "invalid":
        mirror.write_text("{}")
    elif mode == "missing":
        mirror.unlink()
    elif mode == "orphan":
        mirror.write_text(json.dumps(seal(payload(data_health_status="warning"))))
    result = inspect(tmp_path)
    assert result["latest_mirror"]["state"] == mode
    assert result["latest_attempt"]["started_at"] == "2026-09-09T10:30:00Z"


def test_hash_tamper_and_filename_mismatch(tmp_path: Path) -> None:
    target = archive(tmp_path, payload())
    value = json.loads(target.read_text())
    value["successful_instruments"] = 2
    target.write_text(json.dumps(value))
    assert inspect(tmp_path)["latest_attempt"] is None
    target.unlink()
    target = archive(tmp_path, payload())
    target.rename(target.with_name(target.name.replace("10", "11", 1)))
    assert inspect(tmp_path)["latest_attempt"] is None


def test_archive_limit_does_not_guess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import finance_lab.operational_status_io as io

    archive(tmp_path, payload())
    archive(tmp_path, payload(data_health_status="warning"))
    monkeypatch.setattr(io, "MAX_ARCHIVES", 1)
    result = inspect(tmp_path)
    assert result["scan"]["complete"] is False
    assert result["latest_attempt"] is None


def test_mirror_match_uses_full_latest_not_displayed_short_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import finance_lab.paper_automation_history as module

    old = seal(payload())
    archive(tmp_path, old)
    archive(
        tmp_path,
        payload(started_at="2026-09-09T10:30:00Z", completed_at="2026-09-09T10:31:00Z"),
        mirror=False,
    )
    original = module._safe_record

    def same_short_id(value: dict[str, Any]) -> dict[str, Any]:
        result = original(value)
        result["run_id"] = old["run_id"]
        return result

    monkeypatch.setattr(module, "_safe_record", same_short_id)
    assert inspect(tmp_path)["latest_mirror"]["state"] == "behind"


def test_legacy_missing_error_code_is_unknown(tmp_path: Path) -> None:
    failed = payload(
        status="failed",
        failed_step="market_data_update",
        steps=[{"name": "market_data_update", "status": "failed"}],
        error={"type": "OSError", "message": "sensitive legacy exception"},
        operation_status=None,
        processed_dates=[],
        ledger_committed=None,
        successful_instruments=None,
        failed_sources=None,
        market_data_committed=None,
        data_health_status=None,
        data_health_report_path=None,
        paper_report_path=None,
    )
    archive(tmp_path, failed)
    result = inspect(tmp_path)
    assert result["latest_attempt"]["error_code"] is None
    assert result["latest_attempt"]["error_code_origin"] == "unknown"
    assert "sensitive" not in json.dumps(result)


@pytest.mark.parametrize("step", ["paper_run", "paper_init"])
def test_post_health_failure_requires_successful_health(tmp_path: Path, step: str) -> None:
    steps = [
        {"name": "market_data_update", "status": "succeeded"},
        {"name": "data_health", "status": "succeeded"},
    ]
    if step == "paper_init":
        steps.append({"name": "paper_run", "status": "portfolio_missing"})
    steps.append({"name": step, "status": "failed"})
    failed = payload(
        status="failed",
        failed_step=step,
        steps=steps,
        error={"type": "OSError", "code": f"{step}_failed", "message": "failure"},
        operation_status=None,
        processed_dates=[],
        ledger_committed=None,
        data_health_status="error",
        paper_report_path=None,
    )
    archive(tmp_path, failed)
    assert inspect(tmp_path)["latest_attempt"] is None


def test_zero_successful_update_cannot_claim_committed(tmp_path: Path) -> None:
    failed = payload(
        status="failed",
        failed_step="market_data_update",
        steps=[{"name": "market_data_update", "status": "failed"}],
        error={"type": "OSError", "code": "market_data_update_failed", "message": "failure"},
        operation_status=None,
        processed_dates=[],
        ledger_committed=None,
        successful_instruments=0,
        failed_sources=1,
        market_data_committed=True,
        data_health_status=None,
        data_health_report_path=None,
        paper_report_path=None,
    )
    archive(tmp_path, failed)
    assert inspect(tmp_path)["latest_attempt"] is None
