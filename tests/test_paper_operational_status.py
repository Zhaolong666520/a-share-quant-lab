from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_paper_automation_history import archive, payload


def inspect(root: Path, portfolio: str = "study-1") -> dict[str, Any]:
    module = importlib.import_module("finance_lab.paper_operational_status")
    return module.inspect_paper_operational_status(root, portfolio)


def market(root: Path, **changes: Any) -> None:
    summary = changes.pop(
        "summary",
        {
            "status": "success",
            "successful_instruments": 1,
            "failed_sources": 0,
            "records": [{"rows": 3, "source_errors": {}}],
        },
    )
    (root / "data").mkdir(exist_ok=True)
    (root / "outputs").mkdir(exist_ok=True)
    raw = json.dumps(summary).encode()
    (root / "outputs" / "update_summary.json").write_bytes(raw)
    marker = {
        "schema_version": 1,
        "batch_id": "a" * 32,
        "status": "completed",
        "start": "2020-01-01",
        "end": "2026-09-08",
        "symbols": ["sh.510300"],
        "started_at": "2026-09-08T00:00:00Z",
        "phase": "commit",
        "completed_at": "2026-09-08T00:01:00Z",
        "summary_sha256": hashlib.sha256(raw).hexdigest(),
        "error_type": None,
    }
    marker.update(changes)
    (root / "data" / "market_update_state.json").write_text(json.dumps(marker))


def ledger(root: Path) -> Path:
    import pandas as pd

    from finance_lab.ledger import LedgerConfig
    from finance_lab.paper_engine import initialize_account
    from finance_lab.paper_models import PaperDataContext, make_default_accounts
    from finance_lab.paper_store import PaperStore

    history = pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2026-01-02", periods=121),
            "open": [10.0] * 121,
            "close": [10.0] * 121,
        }
    )
    day = history.iloc[-1]["trade_date"].date()
    accounts = make_default_accounts("study-1", day, LedgerConfig())
    context = PaperDataContext(
        "d" * 64,
        "c" * 64,
        "2026-08-31T00:00:00Z",
        day,
        history.iloc[0]["trade_date"].date(),
        day,
        {"synthetic": 1},
        (),
        {},
        0,
        day.isoformat(),
    )
    (root / "data").mkdir(exist_ok=True)
    database = root / "data" / "finance_lab.duckdb"
    with PaperStore(database) as store:
        store.ensure_schema()
        store.initialize_portfolio(
            accounts,
            tuple(initialize_account(a, history) for a in accounts),
            context,
            batch_id="synthetic",
        )
    return database


def fingerprint(root: Path) -> dict[str, Any]:
    return {
        str(p.relative_to(root)): (
            p.read_bytes() if p.is_file() else None,
            p.stat().st_size,
            p.stat().st_mtime_ns,
            p.stat().st_mode,
        )
        for p in root.rglob("*")
    }


def test_empty_root_attention_without_writes(tmp_path: Path) -> None:
    result = inspect(tmp_path)
    assert result["command_exit_code"] == 1
    assert result["automation"]["latest_attempt"] is None
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "outputs").exists()
    assert result["observation"]["consistency"] == "non_atomic"


@pytest.mark.parametrize("portfolio", ["../escape", "", "UPPER", "x--y"])
def test_bad_id_error(tmp_path: Path, portfolio: str) -> None:
    assert inspect(tmp_path, portfolio)["command_exit_code"] == 2


def test_missing_root_error(tmp_path: Path) -> None:
    assert inspect(tmp_path / "missing")["command_exit_code"] == 2


def test_complete_ok_double_read_is_unchanged(tmp_path: Path) -> None:
    archive(tmp_path, payload())
    market(tmp_path)
    ledger(tmp_path)
    before = fingerprint(tmp_path)
    for _ in range(2):
        result = inspect(tmp_path)
        assert result["command_exit_code"] == 0
        assert result["ledger"]["audit_status"] == "passed"
        assert len(result["ledger"]["accounts"]) == 2
        assert result["report"]["generation_state"] == "generated"
        assert result["report"]["artifact_verified"] is False
        assert result["automation_ledger_correlation"] == "not_available"
        assert "private" not in json.dumps(result)
    assert before == fingerprint(tmp_path)


def test_wal_blocks_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = ledger(tmp_path)
    database.with_suffix(database.suffix + ".wal").write_bytes(b"synthetic")
    from finance_lab.paper_store import PaperStore

    monkeypatch.setattr(PaperStore, "__enter__", lambda self: pytest.fail("WAL opened"))
    result = inspect(tmp_path)
    assert result["ledger"]["state"] == "unavailable"
    assert "ledger_wal_present" in [d["code"] for d in result["diagnostics"]]


@pytest.mark.parametrize(
    "summary",
    [
        {
            "status": "success",
            "failed_sources": 1,
            "records": [{"rows": 1, "source_errors": {"source": "failed"}}],
        },
        {"status": "success", "records": [{"rows": True, "source_errors": {}}]},
        {"status": "success", "records": [{"rows": 1, "source_errors": {}, "status": "failed"}]},
        {
            "status": "success",
            "records": [
                {
                    "rows": 1,
                    "source_errors": {},
                    "source_diagnostics": {"source": {"status": "failed"}},
                }
            ],
        },
        {
            "status": "success",
            "records": [{"rows": 1, "source_errors": {"offline_recovery": "historical copy"}}],
        },
    ],
)
def test_summary_conflict_preserves_publication(tmp_path: Path, summary: dict[str, Any]) -> None:
    market(tmp_path, summary=summary)
    result = inspect(tmp_path)
    assert result["market"]["publication_state"] == "completed"
    assert result["market"]["source_outcome"] == "unknown"
    assert "summary_conflict" in [d["code"] for d in result["diagnostics"]]


@pytest.mark.parametrize("status", ["updating", "failed"])
def test_quarantine_observed_without_lock(tmp_path: Path, status: str) -> None:
    market(
        tmp_path,
        status=status,
        phase="fetch",
        completed_at=None,
        summary_sha256=None,
        error_type="OSError" if status == "failed" else None,
    )
    assert inspect(tmp_path)["market"]["isolated"] is True


def test_report_follows_latest_failure_not_success(tmp_path: Path) -> None:
    archive(tmp_path, payload())
    failed = payload(
        started_at="2026-09-09T10:30:00Z",
        completed_at="2026-09-09T10:31:00Z",
        status="failed",
        failed_step="market_data_update",
        steps=[{"name": "market_data_update", "status": "failed"}],
        error={"type": "OSError", "message": "private secret"},
        operation_status=None,
        processed_dates=[],
        ledger_committed=None,
        successful_instruments=0,
        failed_sources=1,
        market_data_committed=False,
        data_health_status=None,
        data_health_report_path=None,
        paper_report_path=None,
    )
    archive(tmp_path, failed)
    result = inspect(tmp_path)
    assert result["report"]["generation_state"] == "not_reached"
    assert result["automation"]["latest_attempt"]["ledger_committed"] is None


def test_subprocess_entry_import_fence_and_exit_codes(tmp_path: Path) -> None:
    code = """import importlib.abc, sys
forbidden = {"finance_lab.cli", "finance_lab.paper_automation", "finance_lab.paper_pipeline",
             "finance_lab.pipeline", "finance_lab.paper_report", "finance_lab.report"}
class Fence(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname in forbidden or fullname.startswith("matplotlib"):
   raise RuntimeError("forbidden import " + fullname)
sys.meta_path.insert(0, Fence())
from finance_lab.paper_operational_status import main
raise SystemExit(main(sys.argv[1:]))"""
    for root, expected in [(tmp_path, 1), (tmp_path / "missing", 2)]:
        process = subprocess.run(
            [sys.executable, "-B", "-c", code, "--root", str(root), "--portfolio", "study-1"],
            capture_output=True,
            text=True,
        )
        assert process.returncode == expected, process.stderr
        assert json.loads(process.stdout)["command_exit_code"] == expected
    archive(tmp_path, payload())
    market(tmp_path)
    ledger(tmp_path)
    process = subprocess.run(
        [sys.executable, "-B", "-c", code, "--root", str(tmp_path), "--portfolio", "study-1"],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["status"] == "ok"


def test_strict_json_rejects_exponent_overflow() -> None:
    from finance_lab.operational_status_io import strict_json

    with pytest.raises(ValueError):
        strict_json(b'{"nested":[1e999]}')


def test_root_parent_traversal_rejected(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    assert inspect(tmp_path / "child" / "..")["command_exit_code"] == 2


@pytest.mark.parametrize(
    "mutation",
    ["missing_version", "future_version", "unknown_table", "event_tamper", "view", "constraint"],
)
def test_ledger_audit_faults_do_not_expose_balances(tmp_path: Path, mutation: str) -> None:
    import duckdb

    database = ledger(tmp_path)
    with duckdb.connect(str(database)) as connection:
        if mutation == "missing_version":
            connection.execute("DELETE FROM paper_schema_metadata")
        elif mutation == "future_version":
            connection.execute("INSERT INTO paper_schema_metadata(schema_version) VALUES (99)")
        elif mutation == "unknown_table":
            connection.execute("CREATE TABLE paper_unknown (x INTEGER)")
        elif mutation == "event_tamper":
            connection.execute("UPDATE paper_events SET event_hash = 'bad'")
        elif mutation == "view":
            connection.execute("ALTER TABLE paper_state RENAME TO saved_state")
            connection.execute("CREATE VIEW paper_state AS SELECT * FROM saved_state")
        elif mutation == "constraint":
            connection.execute("ALTER TABLE paper_state RENAME TO saved_state")
            connection.execute("CREATE TABLE paper_state AS SELECT * FROM saved_state")
            connection.execute("DROP TABLE saved_state")
    before = fingerprint(tmp_path)
    result = inspect(tmp_path)
    assert result["ledger"]["state"] == "unavailable"
    assert result["ledger"]["accounts"] == []
    assert before == fingerprint(tmp_path)


def test_portfolio_missing_does_not_initialize(tmp_path: Path) -> None:
    ledger(tmp_path)
    assert inspect(tmp_path, "missing")["ledger"]["state"] == "unavailable"


def test_permission_error_keeps_other_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive(tmp_path, payload())
    market(tmp_path)
    from finance_lab.operational_status_io import Observation

    original = Observation.read

    def denied(self: Any, path: Path, section: str, **kw: Any) -> Any:
        if path.name == "finance_lab.duckdb":
            raise PermissionError("private detail")
        return original(self, path, section, **kw)

    monkeypatch.setattr(Observation, "read", denied)
    result = inspect(tmp_path)
    assert result["market"]["source_outcome"] == "success"
    assert result["ledger"]["state"] == "unavailable"
    assert "private detail" not in json.dumps(result)


def test_changed_archive_set_unknown_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive(tmp_path, payload())
    import finance_lab.paper_operational_status as module

    original = module._ledger

    def changed(*args: Any) -> Any:
        value = original(*args)
        archive(tmp_path, payload(data_health_status="warning"), mirror=False)
        return value

    monkeypatch.setattr(module, "_ledger", changed)
    result = inspect(tmp_path)
    assert result["observation"]["input_changed"] is True
    assert result["automation"]["state"] == "changed"
    assert result["automation"]["latest_attempt"] is None
    assert result["report"]["generation_state"] == "unknown"


def test_changed_marker_unknown_market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    market(tmp_path)
    import finance_lab.paper_operational_status as module

    original = module._ledger

    def changed(*args: Any) -> Any:
        value = original(*args)
        market(tmp_path, status="updating", phase="fetch", completed_at=None, summary_sha256=None)
        return value

    monkeypatch.setattr(module, "_ledger", changed)
    result = inspect(tmp_path)
    assert result["market"]["state"] == "changed"
    assert result["market"]["source_outcome"] == "unknown"


def test_safe_reader_rejects_large_json(tmp_path: Path) -> None:
    from finance_lab.operational_status_io import Observation

    path = tmp_path / "large.json"
    path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        Observation(tmp_path).read(path, "market")


def test_symlink_root_and_marker_rejected(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    assert inspect(link)["command_exit_code"] == 2
    market(destination)
    marker = destination / "data" / "market_update_state.json"
    saved = destination / "saved.json"
    marker.rename(saved)
    marker.symlink_to(saved)
    assert inspect(destination)["market"]["state"] == "unavailable"


def test_unreadable_summary_preserves_valid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market(tmp_path)
    from finance_lab.operational_status_io import Observation

    original = Observation.read

    def denied(self: Any, path: Path, section: str, **kw: Any) -> Any:
        if path.name == "update_summary.json":
            raise PermissionError("private summary path")
        return original(self, path, section, **kw)

    monkeypatch.setattr(Observation, "read", denied)
    result = inspect(tmp_path)
    assert result["market"]["publication_state"] == "completed"
    assert result["market"]["source_outcome"] == "unknown"
    assert result["market"]["summary_state"] == "unavailable"
    assert result["command_exit_code"] == 1


@pytest.mark.parametrize("record_status", ["failed", None])
def test_zero_row_record_cannot_be_hidden_by_summary_success(
    tmp_path: Path,
    record_status: str | None,
) -> None:
    empty: dict[str, Any] = {"rows": 0, "source_errors": {}}
    if record_status is not None:
        empty["status"] = record_status
    market(
        tmp_path,
        summary={
            "status": "success",
            "successful_instruments": 1,
            "failed_sources": 0,
            "records": [{"rows": 10, "source_errors": {}, "status": "success"}, empty],
        },
    )
    result = inspect(tmp_path)
    assert result["market"]["source_outcome"] == "unknown"
    assert "summary_conflict" in [d["code"] for d in result["diagnostics"]]


@pytest.mark.parametrize(
    "counts",
    [
        {"received_rows": 1, "accepted_rows": 999, "rejected_rows": 5},
        {"received_rows": 10, "accepted_rows": 9, "rejected_rows": 5},
        {"received_rows": 10, "accepted_rows": True, "rejected_rows": 0},
        {"received_rows": 10},
        {},
    ],
)
def test_invalid_source_diagnostic_counts_are_unknown(
    tmp_path: Path,
    counts: dict[str, Any],
) -> None:
    detail = {"status": "success", **counts}
    market(
        tmp_path,
        summary={
            "status": "success",
            "records": [{"rows": 1, "source_errors": {}, "source_diagnostics": {"source": detail}}],
        },
    )
    result = inspect(tmp_path)
    assert result["market"]["source_outcome"] == "unknown"
    assert result["market"]["publication_state"] == "completed"


@pytest.mark.parametrize("variant", ["legacy", "missing_summary", "mismatch", "invalid_marker"])
def test_market_incomplete_publication_variants(tmp_path: Path, variant: str) -> None:
    market(tmp_path)
    marker = tmp_path / "data" / "market_update_state.json"
    summary = tmp_path / "outputs" / "update_summary.json"
    if variant == "legacy":
        marker.unlink()
    elif variant == "missing_summary":
        summary.unlink()
    elif variant == "mismatch":
        summary.write_text("{}")
    else:
        marker.write_text("{}")
    result = inspect(tmp_path)
    assert result["market"]["source_outcome"] == "unknown"
    assert result["command_exit_code"] == 1
    assert result["market"]["publication_state"] == (
        "legacy"
        if variant == "legacy"
        else "unknown"
        if variant == "invalid_marker"
        else "completed"
    )


def test_divergent_actual_valuation_dates_are_not_a_common_date(tmp_path: Path) -> None:
    import pandas as pd

    from finance_lab.paper_engine import advance_one_bar
    from finance_lab.paper_models import PaperDataContext
    from finance_lab.paper_store import PaperStore

    database = ledger(tmp_path)
    history = pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2026-01-02", periods=122),
            "open": [10.0] * 122,
            "close": [10.0] * 122,
        }
    )
    day = history.iloc[-1]["trade_date"].date()
    context = PaperDataContext(
        "e" * 64,
        "f" * 64,
        "2026-08-31T00:00:00Z",
        day,
        history.iloc[0]["trade_date"].date(),
        day,
        {"synthetic": 1},
        (),
        {},
        0,
        day.isoformat(),
    )
    with PaperStore(database) as store:
        accounts = store.load_accounts("study-1")
        states = {s.account_id: s for s in store.load_states("study-1")}
        first = accounts[0]
        step = advance_one_bar(first, states[first.account_id], history)
        store.commit_batch((first,), (step,), context, batch_id="synthetic-one-account")
    result = inspect(tmp_path)
    assert result["ledger"]["state"] == "available"
    assert result["ledger"]["last_valuation_date"] is None
    dates = {a["last_valuation_date"] for a in result["ledger"]["accounts"]}
    assert len(dates) == 2
    assert "ledger_valuation_dates_unknown_or_different" in [
        d["code"] for d in result["diagnostics"]
    ]
