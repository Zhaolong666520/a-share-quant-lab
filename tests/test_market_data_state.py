from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance_lab import pipeline, storage
from finance_lab.data_manifest import generate_dataset_manifest, read_update_summary
from finance_lab.market_data_lock import market_data_lock
from finance_lab.paper_pipeline import load_paper_market_snapshot
from tests.paper_helpers import make_paper_test_project


def _install_fetch(monkeypatch: pytest.MonkeyPatch, prices: pd.DataFrame) -> None:
    monkeypatch.setattr(pipeline, "fetch_akshare", lambda *args: prices.copy())
    monkeypatch.setattr(pipeline, "fetch_baostock", lambda *args: prices.copy())


def _update(root: Path, prices: pd.DataFrame) -> dict[str, object]:
    return pipeline.update_market_data(date(2022, 1, 1), prices["trade_date"].max().date(), root)


def test_summary_failure_quarantines_already_published_prices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    previous_summary = (paths.outputs / "update_summary.json").read_bytes()
    updated = prices.copy()
    updated["volume"] += 1
    updated["ingested_at"] = "2030-01-01T00:00:00Z"

    def fetch(*args: object) -> pd.DataFrame:
        return updated

    def interrupted_summary(*args: object, **kwargs: object) -> None:
        raise OSError("summary interrupted")

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    monkeypatch.setattr(pipeline, "write_text_atomic", interrupted_summary)
    with pytest.raises(OSError, match="summary interrupted"):
        pipeline.update_market_data(
            date(2022, 1, 1),
            prices["trade_date"].max().date(),
            tmp_path,
        )
    actual = pd.read_parquet(paths.curated / "sh_510300.parquet")
    assert list(actual["volume"]) == list(updated["volume"])
    assert (paths.outputs / "update_summary.json").read_bytes() == previous_summary
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)


def _state_payload() -> dict[str, object]:
    return {
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


@pytest.mark.parametrize(
    "operation",
    [
        "raw",
        "curated",
        "sync",
        "read",
        "manifest",
        "summary",
        "validate",
        "update",
        "paper",
    ],
)
@pytest.mark.parametrize("status", ["updating", "failed"])
def test_incomplete_batch_blocks_public_operations_even_when_caller_holds_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status: str,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    last = prices["trade_date"].max().date()
    payload = _state_payload()
    payload["status"] = status
    payload["error_type"] = "OSError" if status == "failed" else None
    state_path = paths.data / "market_update_state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    calls: list[str] = []

    def fetch(*args: object) -> pd.DataFrame:
        calls.append("fetch")
        return prices

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    actions: dict[str, Callable[[], object]] = {
        "raw": lambda: storage.save_raw_snapshot(prices, paths),
        "curated": lambda: storage.save_curated(prices, paths),
        "sync": lambda: storage.sync_duckdb(paths),
        "read": lambda: storage.read_curated("sh.510300", paths),
        "manifest": lambda: generate_dataset_manifest(paths, as_of_date=last),
        "summary": lambda: read_update_summary(paths),
        "validate": lambda: pipeline.validate_curated_data(tmp_path),
        "update": lambda: pipeline.update_market_data(date(2022, 1, 1), last, tmp_path),
        "paper": lambda: load_paper_market_snapshot(paths, last, 3),
    }
    with market_data_lock(paths.data):
        with pytest.raises(RuntimeError, match="行情更新"):
            actions[operation]()
    assert calls == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"status": "ok"},
        {"batch_id": "../bad"},
        {"start": "2022-99-99"},
        {"end": "2020-01-01"},
        {"symbols": ["SH.510300"]},
        {"symbols": ["sh.510300", "sh.510300"]},
        {"started_at": "2022-01-01"},
        {"phase": "unknown"},
        {"unexpected": 1},
        {"error_type": "OSError"},
        {"status": "failed"},
        {"status": "completed"},
    ],
)
def test_malformed_state_is_not_legacy(tmp_path: Path, change: dict[str, object]) -> None:
    from finance_lab.market_data_state import market_update_status

    paths, _ = make_paper_test_project(tmp_path)
    payload = {**_state_payload(), **change}
    (paths.data / "market_update_state.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)
    assert market_update_status(paths)["status"] == "invalid"


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"status":"completed","status":"updating"}'])
def test_unparseable_state_blocks_read(tmp_path: Path, content: str) -> None:
    paths, _ = make_paper_test_project(tmp_path)
    (paths.data / "market_update_state.json").write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)


def test_completed_batch_binds_summary_bytes(tmp_path: Path) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    summary = paths.outputs / "update_summary.json"
    payload = {
        **_state_payload(),
        "status": "completed",
        "phase": "commit",
        "completed_at": "2022-07-15T10:00:01+00:00",
        "summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest(),
    }
    (paths.data / "market_update_state.json").write_text(json.dumps(payload), encoding="utf-8")
    assert len(storage.read_curated("sh.510300", paths)) == len(prices)
    summary.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)


@pytest.mark.parametrize("phase", ["preparing", "raw", "database", "summary", "completed"])
def test_state_write_failure_never_opens_an_incomplete_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    from finance_lab import market_data_state as state

    paths, prices = make_paper_test_project(tmp_path)
    _install_fetch(monkeypatch, prices)
    previous = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    original = state.write_text_atomic

    def fail(path: Path, text: str, **kwargs: object) -> None:
        payload = json.loads(text)
        if payload["status"] == phase or (
            payload["status"] == "updating" and payload["phase"] == phase
        ):
            raise OSError("state write interrupted")
        original(path, text)

    monkeypatch.setattr(state, "write_text_atomic", fail)
    with pytest.raises(OSError, match="state write interrupted"):
        _update(tmp_path, prices)
    if phase == "preparing":
        assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == previous
        assert state.market_update_status(paths)["status"] == "legacy"
    else:
        diagnostic = state.market_update_status(paths)
        assert diagnostic["status"] == "failed"
        assert diagnostic["blocked"]
        with pytest.raises(state.MarketDataStateError):
            storage.save_curated(prices, paths)


@pytest.mark.parametrize("secondary_failure", [False, True])
def test_database_failure_preserves_primary_error_and_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secondary_failure: bool,
) -> None:
    from finance_lab import market_data_state as state

    paths, prices = make_paper_test_project(tmp_path)
    _install_fetch(monkeypatch, prices)
    original = state.write_text_atomic

    def failed_state(path: Path, text: str, **kwargs: object) -> None:
        if secondary_failure and json.loads(text)["status"] == "failed":
            raise OSError("secondary failure")
        original(path, text)

    def failed_database(*args: object) -> int:
        raise OSError("primary database failure")

    monkeypatch.setattr(state, "write_text_atomic", failed_state)
    monkeypatch.setattr(pipeline, "sync_duckdb", failed_database)
    with pytest.raises(OSError, match="primary database failure"):
        _update(tmp_path, prices)
    result = state.market_update_status(paths)
    assert result["status"] == ("updating" if secondary_failure else "failed")
    assert result["blocked"]
    with pytest.raises(state.MarketDataStateError):
        _update(tmp_path, prices)


def test_successful_batches_bind_summary_and_release_internal_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab import market_data_state as state

    paths, prices = make_paper_test_project(tmp_path)
    _install_fetch(monkeypatch, prices)
    _update(tmp_path, prices)
    first = state.market_update_status(paths)
    assert first["status"] == "completed" and not first["blocked"]
    _update(tmp_path, prices)
    second = state.market_update_status(paths)
    assert first["state"]["batch_id"] != second["state"]["batch_id"]
    assert len(storage.read_curated("sh.510300", paths)) == len(prices)
    (paths.outputs / "update_summary.json").unlink()
    assert state.market_update_status(paths)["reason"] == "summary_unreadable"
    with pytest.raises(state.MarketDataStateError):
        storage.save_curated(prices, paths)


def test_active_writer_cannot_read_a_half_published_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.market_data_state import MarketDataStateError

    paths, prices = make_paper_test_project(tmp_path)
    checked: list[str] = []

    def fetch(*args: object) -> pd.DataFrame:
        with pytest.raises(MarketDataStateError):
            storage.read_curated("sh.510300", paths)
        with pytest.raises(MarketDataStateError):
            _update(tmp_path, prices)
        checked.append("blocked")
        return prices

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    _update(tmp_path, prices)
    assert checked == ["blocked", "blocked"]


def test_source_failures_are_not_washed_into_healthy_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.market_data_state import market_update_status
    from finance_lab.sources import DataSourceError

    paths, prices = make_paper_test_project(tmp_path)

    def fail(*args: object) -> pd.DataFrame:
        raise DataSourceError("upstream unavailable")

    monkeypatch.setattr(pipeline, "fetch_akshare", fail)
    monkeypatch.setattr(pipeline, "fetch_baostock", fail)
    result = _update(tmp_path, prices)
    assert result["successful_instruments"] == 0
    assert market_update_status(paths)["status"] == "completed"
    assert read_update_summary(paths).records[0].source_errors == {
        "akshare": "akshare 数据源在 unknown 阶段失败",
        "baostock": "baostock 数据源在 unknown 阶段失败",
    }
    with pytest.raises(RuntimeError, match="没有有效数据行"):
        load_paper_market_snapshot(paths, prices["trade_date"].max().date(), 3)


@pytest.mark.parametrize("status", ["legacy", "completed", "failed", "invalid"])
def test_status_cli_is_read_only_and_has_meaningful_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    from finance_lab.cli import main

    paths, prices = make_paper_test_project(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    _install_fetch(monkeypatch, prices)
    state_path = paths.data / "market_update_state.json"
    if status == "completed":
        _update(tmp_path, prices)
    elif status == "failed":
        state_path.write_text(
            json.dumps({**_state_payload(), "status": "failed", "error_type": "OSError"}),
            encoding="utf-8",
        )
    elif status == "invalid":
        state_path.write_text("{ secret should not be echoed", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert main(["data-update-status"]) == (1 if status in {"failed", "invalid"} else 0)
    result = capsys.readouterr()
    assert "secret" not in result.out
    assert json.loads(result.out)["status"] == status
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("phase", ["curated", "database", "summary"])
def test_process_death_after_publication_is_still_blocked(tmp_path: Path, phase: str) -> None:
    from finance_lab.market_data_state import MarketDataStateError, market_update_status

    paths, _ = make_paper_test_project(tmp_path)
    code = f"""
import os
from datetime import date
from finance_lab import pipeline
from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
prices = make_synthetic_daily_prices(periods=145)
prices['symbol'] = 'sh.510300'
prices['source'] = 'akshare'
prices['volume_unit'] = 'share'
pipeline.fetch_akshare = lambda *args: prices.copy()
pipeline.fetch_baostock = lambda *args: prices.copy()
name = {{'curated': 'save_curated', 'database': 'sync_duckdb',
        'summary': 'write_text_atomic'}}[{phase!r}]
original = getattr(pipeline, name)
def interrupted(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(7)
setattr(pipeline, name, interrupted)
pipeline.update_market_data(date(2022, 1, 1), prices['trade_date'].max().date())
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        env={
            **os.environ,
            "FINANCE_LAB_ROOT": str(tmp_path),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 7, result.stderr
    assert len(pd.read_parquet(paths.curated / "sh_510300.parquet")) == 145
    assert market_update_status(paths)["status"] == "updating"
    with pytest.raises(MarketDataStateError):
        storage.read_curated("sh.510300", paths)


def test_daily_automation_stops_at_quarantine_and_preserves_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.paper_automation import run_paper_daily_automation
    from finance_lab.paper_pipeline import paper_init_portfolio

    paths, prices = make_paper_test_project(tmp_path)
    last = prices["trade_date"].max().date()
    paper_init_portfolio(as_of_date=last, root=tmp_path)
    before = paths.database.read_bytes()
    state_path = paths.data / "market_update_state.json"
    state_path.write_text(json.dumps(_state_payload()), encoding="utf-8")
    original_state = state_path.read_bytes()

    def forbidden(*args: object) -> pd.DataFrame:
        raise AssertionError("fetch must not start")

    monkeypatch.setattr(pipeline, "fetch_akshare", forbidden)
    monkeypatch.setattr(pipeline, "fetch_baostock", forbidden)
    result = run_paper_daily_automation(as_of_date=last, root=tmp_path)
    assert result.exit_code == 1
    assert result.payload["failed_step"] == "market_data_update"
    assert result.payload["error"]["type"] == "MarketDataStateError"
    assert result.payload["operation_status"] is None
    assert json.loads(result.archive_path.read_text(encoding="utf-8")) == result.payload
    assert paths.database.read_bytes() == before
    assert state_path.read_bytes() == original_state


@pytest.mark.parametrize("legacy", [False, True])
def test_paper_report_only_gates_the_legacy_curated_fallback(tmp_path: Path, legacy: bool) -> None:
    from finance_lab.market_data_state import MarketDataStateError
    from finance_lab.paper_pipeline import paper_init_portfolio
    from finance_lab.paper_report import _attribution_prices, _query_frame
    from finance_lab.paper_store import PaperStore

    paths, prices = make_paper_test_project(tmp_path)
    paper_init_portfolio(as_of_date=prices["trade_date"].max().date(), root=tmp_path)
    with PaperStore(paths.database, read_only=True) as store:
        snapshot = store.portfolio_snapshot("default")
    events = _query_frame(paths.database, "SELECT * FROM paper_events", [])
    if legacy:
        for index, row in events.iterrows():
            payload = json.loads(row["payload_json"])
            payload.pop("open_price", None)
            events.at[index, "payload_json"] = json.dumps(payload)
    assert len(_attribution_prices(paths, snapshot, events)) == 2
    before = paths.database.read_bytes()
    (paths.data / "market_update_state.json").write_text(
        json.dumps(_state_payload()),
        encoding="utf-8",
    )
    if legacy:
        with pytest.raises(MarketDataStateError):
            _attribution_prices(paths, snapshot, events)
    else:
        assert len(_attribution_prices(paths, snapshot, events)) == 2
    assert paths.database.read_bytes() == before


def test_batch_permission_is_not_reusable_after_exit_or_state_replacement(tmp_path: Path) -> None:
    from finance_lab.market_data_state import MarketDataStateError, market_update_batch

    paths, prices = make_paper_test_project(tmp_path)
    with pytest.raises(MarketDataStateError):
        with market_update_batch(
            paths,
            date(2022, 1, 1),
            prices["trade_date"].max().date(),
            ("sh.510300",),
        ) as batch:
            state_path = paths.data / "market_update_state.json"
            replacement = json.loads(state_path.read_text(encoding="utf-8"))
            replacement["batch_id"] = "b" * 32
            state_path.write_text(json.dumps(replacement), encoding="utf-8")
            batch.stage("curated")
    assert json.loads(state_path.read_text(encoding="utf-8")) == replacement
    with pytest.raises(MarketDataStateError):
        batch.stage("curated")
    with pytest.raises(MarketDataStateError):
        storage.save_curated(prices, paths)
