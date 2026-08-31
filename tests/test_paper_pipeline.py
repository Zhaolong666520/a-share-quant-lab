from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

import finance_lab.paper_pipeline as paper_pipeline_module
from finance_lab.config import ProjectPaths
from finance_lab.paper_pipeline import (
    PaperDataGateError,
    PaperPortfolioNotFound,
    load_paper_market_snapshot,
    paper_init_portfolio,
    paper_run_portfolio,
    paper_status_portfolio,
)
from finance_lab.paper_store import PaperPortfolioExistsError
from finance_lab.storage import save_curated
from tests.paper_helpers import make_paper_test_project, write_update_summary


def test_data_gate_allows_disclosed_price_warnings_and_preserves_lineage(
    tmp_path: Path,
) -> None:
    paths, prices = make_paper_test_project(tmp_path, mixed_sources=True)
    as_of_date = prices["trade_date"].max().date()
    write_update_summary(
        paths,
        end=as_of_date.isoformat(),
        rows=len(prices),
        curated_file="data/curated/sh_510300.parquet",
        source_errors={"akshare": "temporary upstream failure"},
    )

    snapshot = load_paper_market_snapshot(paths, as_of_date, stale_after_business_days=3)

    assert snapshot.prices["symbol"].eq("sh.510300").all()
    assert {"unadjusted_prices", "mixed_sources"}.issubset(
        snapshot.data_context.warning_codes
    )
    assert snapshot.data_context.upstream_errors == {
        "akshare": "temporary upstream failure"
    }
    assert snapshot.data_context.dataset_id
    assert len(snapshot.data_context.curated_sha256) == 64
    assert not paths.database.exists()


@pytest.mark.parametrize("failure_mode", ["stale", "malformed", "zero_rows", "end_mismatch"])
def test_data_gate_blocks_untrusted_update_context_without_database_mutation(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    last_date = prices["trade_date"].max().date()
    as_of_date = last_date
    if failure_mode == "stale":
        as_of_date = last_date + timedelta(days=10)
        write_update_summary(
            paths,
            end=as_of_date.isoformat(),
            rows=len(prices),
            curated_file="data/curated/sh_510300.parquet",
        )
    elif failure_mode == "malformed":
        (paths.outputs / "update_summary.json").write_text("{", encoding="utf-8")
    elif failure_mode == "zero_rows":
        write_update_summary(
            paths,
            end=as_of_date.isoformat(),
            rows=0,
            curated_file="data/curated/sh_510300.parquet",
        )
    else:
        write_update_summary(
            paths,
            end=(as_of_date - timedelta(days=1)).isoformat(),
            rows=len(prices),
            curated_file="data/curated/sh_510300.parquet",
        )

    with pytest.raises(PaperDataGateError):
        load_paper_market_snapshot(paths, as_of_date, stale_after_business_days=3)

    assert not paths.database.exists()


def test_data_gate_rejects_curated_file_replaced_after_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    as_of_date = prices["trade_date"].max().date()
    original_hash = paper_pipeline_module.file_sha256
    observed_hashes = iter([original_hash(paths.curated / "sh_510300.parquet"), "0" * 64])
    monkeypatch.setattr(paper_pipeline_module, "file_sha256", lambda _path: next(observed_hashes))

    with pytest.raises(PaperDataGateError, match="发生变化"):
        load_paper_market_snapshot(paths, as_of_date, stale_after_business_days=3)

    assert not paths.database.exists()


def test_data_gate_blocks_calendar_snapshot_fallback_without_database_mutation(
    tmp_path: Path,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    prices["trade_date"] = pd.bdate_range(end="2026-12-31", periods=len(prices))
    prices.to_parquet(paths.curated / "sh_510300.parquet", index=False)
    as_of_date = date(2027, 1, 5)
    write_update_summary(
        paths,
        end=as_of_date.isoformat(),
        rows=len(prices),
        curated_file="data/curated/sh_510300.parquet",
    )

    with pytest.raises(PaperDataGateError, match="calendar_snapshot_out_of_range"):
        load_paper_market_snapshot(paths, as_of_date, stale_after_business_days=3)

    assert not paths.database.exists()


def _append_rising_bars(
    paths: ProjectPaths,
    prices: pd.DataFrame,
    count: int,
) -> tuple[pd.DataFrame, date]:
    updated = prices.copy()
    for _ in range(count):
        row = updated.tail(1).copy()
        next_date = pd.Timestamp(row.iloc[0]["trade_date"]) + pd.offsets.BDay(1)
        next_close = float(row.iloc[0]["close"]) + 1.0
        row["trade_date"] = next_date
        row["open"] = next_close
        row["close"] = next_close
        row["high"] = next_close * 1.01
        row["low"] = next_close * 0.99
        row["amount"] = row["volume"] * next_close
        updated = pd.concat([updated, row], ignore_index=True)
    save_curated(updated.iloc[len(prices) :], paths)
    end_date = updated["trade_date"].max().date()
    write_update_summary(
        paths,
        end=end_date.isoformat(),
        rows=count,
        curated_file="data/curated/sh_510300.parquet",
    )
    return updated, end_date


def test_paper_init_creates_two_forward_only_accounts(tmp_path: Path) -> None:
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    as_of_date = prices["trade_date"].max().date()

    result = paper_init_portfolio(root=tmp_path, as_of_date=as_of_date)

    assert result.status == "initialized"
    assert result.processed_dates == (as_of_date,)
    assert len(result.states) == 2
    assert all(state.last_trade_date == as_of_date for state in result.states)
    assert all(state.shares == 0 for state in result.states)
    assert all(state.pending_order is not None for state in result.states)
    assert result.report_path is not None
    assert result.report_path.exists()
    with duckdb.connect(str(paths.database), read_only=True) as connection:
        filled_orders = connection.execute(
            "SELECT COUNT(*) FROM paper_events WHERE event_type = 'ORDER_FILLED'"
        ).fetchone()
        runs = connection.execute("SELECT COUNT(*) FROM paper_runs").fetchone()
    assert filled_orders == (0,)
    assert runs == (2,)

    with pytest.raises(PaperPortfolioExistsError, match="已存在"):
        paper_init_portfolio(root=tmp_path, as_of_date=as_of_date)


def test_paper_run_processes_each_unseen_bar_once_and_then_becomes_no_op(
    tmp_path: Path,
) -> None:
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    created_on = prices["trade_date"].max().date()
    paper_init_portfolio(root=tmp_path, as_of_date=created_on)
    updated, latest_date = _append_rising_bars(paths, prices, count=3)

    result = paper_run_portfolio(root=tmp_path, as_of_date=latest_date)

    assert result.status == "processed"
    assert result.processed_dates == tuple(
        item.date() for item in updated["trade_date"].tail(3)
    )
    with duckdb.connect(str(paths.database), read_only=True) as connection:
        filled_orders = connection.execute(
            "SELECT COUNT(*) FROM paper_events WHERE event_type = 'ORDER_FILLED'"
        ).fetchone()
        runs_before_retry = connection.execute("SELECT COUNT(*) FROM paper_runs").fetchone()
    assert filled_orders == (2,)
    assert runs_before_retry == (8,)

    retried = paper_run_portfolio(root=tmp_path, as_of_date=latest_date)

    assert retried.status == "no-op"
    assert retried.processed_dates == ()
    with duckdb.connect(str(paths.database), read_only=True) as connection:
        runs_after_retry = connection.execute("SELECT COUNT(*) FROM paper_runs").fetchone()
    assert runs_after_retry == runs_before_retry


def test_paper_status_is_read_only_and_missing_portfolio_is_explicit(tmp_path: Path) -> None:
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    as_of_date = prices["trade_date"].max().date()

    with pytest.raises(PaperPortfolioNotFound, match="不存在"):
        paper_status_portfolio(root=tmp_path)

    initialized = paper_init_portfolio(root=tmp_path, as_of_date=as_of_date)
    before_mtime = paths.database.stat().st_mtime_ns
    status = paper_status_portfolio(root=tmp_path)

    assert status.status == "status"
    assert status.states == initialized.states
    assert paths.database.stat().st_mtime_ns == before_mtime


def test_report_failure_does_not_rollback_committed_paper_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    as_of_date = prices["trade_date"].max().date()

    def fail_report(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("injected report failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(paper_pipeline_module, "write_paper_report", fail_report)
        with pytest.raises(RuntimeError, match="injected report failure"):
            paper_init_portfolio(root=tmp_path, as_of_date=as_of_date)

    status = paper_status_portfolio(root=tmp_path)
    assert status.status == "status"
