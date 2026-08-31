from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

import finance_lab.paper_pipeline as paper_pipeline_module
from finance_lab.paper_pipeline import PaperDataGateError, load_paper_market_snapshot
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
