"""Synthetic evidence changes during a mocked scheduler query, never real market data."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from tests.test_paper_automation_history import archive, payload
from tests.test_paper_operational_status import fingerprint, ledger, market
from tests.test_paper_runtime_status import NOW, log, scheduler


@pytest.mark.parametrize("query_available", [True, False])
@pytest.mark.parametrize("change", ["none", "archives", "summary", "wal", "database", "marker"])
def test_runtime_final_check_covers_recorded_evidence(
    tmp_path, monkeypatch, change, query_available
):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    archive(tmp_path, payload())
    market(tmp_path)
    ledger(tmp_path)
    log(
        tmp_path,
        "[2026-09-08T10:29:59Z] status=started as_of=2026-09-08 portfolio=study-1\n"
        "[2026-09-08T10:31:01Z] status=succeeded\n",
    )
    after_mutation = None

    def query(*args):
        nonlocal after_mutation
        if change == "archives":
            archive(
                tmp_path,
                payload(started_at="2026-09-09T10:30:00Z", completed_at="2026-09-09T10:31:00Z"),
            )
        elif change == "summary":
            path = tmp_path / "outputs/update_summary.json"
            value = json.loads(path.read_text())
            value["failed_sources"] = 1
            path.write_text(json.dumps(value))
        elif change == "wal":
            (tmp_path / "data/finance_lab.duckdb.wal").write_bytes(b"synthetic-test-wal")
        elif change == "database":
            with (tmp_path / "data/finance_lab.duckdb").open("ab") as stream:
                stream.write(b"synthetic-changed-by-test")
        elif change == "marker":
            path = tmp_path / "data/market_update_state.json"
            value = json.loads(path.read_text())
            value["batch_id"] = "b" * 32
            path.write_text(json.dumps(value))
        # The final comparison must fingerprint evidence, not reopen a changed database.
        from finance_lab.paper_store import PaperStore

        monkeypatch.setattr(PaperStore, "__enter__", lambda self: pytest.fail("database reopened"))
        after_mutation = fingerprint(tmp_path)
        return scheduler(trigger_origin="manual") if query_available else {"status": "unknown"}

    monkeypatch.setattr(module, "read_scheduler_status", query)
    result = module.inspect_paper_runtime_status(tmp_path, "study-1", "2026-01-02", now=NOW)
    assert fingerprint(tmp_path) == after_mutation  # The diagnostic itself wrote nothing.
    assert result["observation"]["runtime_input_changed"] is (change != "none")
    if change == "none":
        assert result["recorded_status"]["ledger"]["audit_status"] == "passed"
        return
    assert result["command_exit_code"] == 1
    assert "input_changed" in {item["code"] for item in result["diagnostics"]}
    if change == "archives":
        assert result["application"]["latest_archived_attempt"] is None
        assert result["application"]["last_success"] is None
        assert result["progress"]["application"] == "unknown"
        assert result["application"]["unarchived_start"] == "unknown"
        assert result["recorded_status"]["automation"]["scan"]["complete"] is False
        assert result["recorded_status"]["automation"]["latest_candidates"] == []
        assert result["recorded_status"]["automation"]["last_success_candidates"] == []
        assert result["recorded_status"]["report"]["generation_state"] != "generated"
    elif change in {"summary", "marker"}:
        assert result["recorded_status"]["market"]["source_outcome"] == "unknown"
        assert result["market_update"]["state"] == "changed"
        observed_market = result["recorded_status"]["market"]
        for field in ("successful_instruments", "failed_sources", "offline_recovery_hint",
                      "batch_id", "phase"):
            assert observed_market[field] is None
        for field in ("source_outcome_origin", "successful_instruments_origin",
                      "failed_sources_origin"):
            assert observed_market[field] == "unknown"
    else:
        assert result["recorded_status"]["ledger"]["audit_status"] == "unknown"
        assert result["progress"]["valuation"] == "unknown"


@pytest.mark.parametrize("wrong", ["root", "type"])
def test_operational_shared_observer_cannot_mix_roots(tmp_path: Path, wrong: str):
    from finance_lab.operational_status_io import Observation
    from finance_lab.paper_operational_status import inspect_paper_operational_status

    observer = Observation(tmp_path / "other") if wrong == "root" else object()
    result = inspect_paper_operational_status(tmp_path, "study-1", _observer=observer)
    assert result["command_exit_code"] == 2
    assert list(tmp_path.iterdir()) == []


def test_marker_change_between_runtime_and_recorded_reads_is_not_forgotten(tmp_path, monkeypatch):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    archive(tmp_path, payload())
    market(tmp_path)
    ledger(tmp_path)
    original = module.inspect_paper_operational_status

    def mutate_before_recorded(*args, **kwargs):
        path = tmp_path / "data/market_update_state.json"
        value = json.loads(path.read_text())
        value["end"] = "2026-09-09"
        path.write_text(json.dumps(value))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "inspect_paper_operational_status", mutate_before_recorded)
    monkeypatch.setattr(module, "read_scheduler_status", lambda *a: scheduler())
    result = module.inspect_paper_runtime_status(tmp_path, "study-1", "2026-01-02", now=NOW)
    assert result["observation"]["runtime_input_changed"] is True
    assert result["market_update"]["state"] == "changed"


def test_unrelated_portfolio_log_does_not_invalidate_target(tmp_path, monkeypatch):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    archive(tmp_path, payload())
    market(tmp_path)
    ledger(tmp_path)
    target_log = log(tmp_path, "[2026-09-08T10:31:01Z] status=succeeded\n")

    def unrelated(*args):
        (target_log.parent / "other-2026-10.log").write_text("synthetic unrelated log")
        return scheduler()

    monkeypatch.setattr(module, "read_scheduler_status", unrelated)
    result = module.inspect_paper_runtime_status(tmp_path, "study-1", "2026-01-02", now=NOW)
    assert result["observation"]["runtime_input_changed"] is False
    assert result["recorded_status"]["ledger"]["audit_status"] == "passed"
