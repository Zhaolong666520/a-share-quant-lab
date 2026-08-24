from __future__ import annotations

from pathlib import Path

import duckdb

from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import read_curated, save_curated, sync_duckdb


def test_parquet_and_duckdb_round_trip(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=150)
    save_curated(frame, paths)
    assert sync_duckdb(paths) == 150
    restored = read_curated("demo.000300", paths)
    assert len(restored) == 150
    with duckdb.connect(str(paths.database), read_only=True) as connection:
        count = connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
    assert count == 150


def test_fallback_source_does_not_overwrite_preferred_source(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    preferred = make_synthetic_daily_prices(periods=150)
    preferred["source"] = "akshare"
    fallback = preferred.tail(20).copy()
    fallback["source"] = "baostock"
    fallback["amount"] = fallback["amount"] + 10

    save_curated(preferred, paths)
    save_curated(fallback, paths)
    restored = read_curated("demo.000300", paths)

    assert len(restored) == 150
    assert set(restored["source"]) == {"akshare"}
