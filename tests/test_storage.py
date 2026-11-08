from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from finance_lab import storage
from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import (
    read_curated,
    save_curated,
    save_raw_snapshot,
    sync_duckdb,
)


def test_parquet_and_duckdb_round_trip(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=150)
    save_curated(frame, paths)
    assert sync_duckdb(paths) == 150
    restored = read_curated("demo.000300", paths)
    assert len(restored) == 150
    with duckdb.connect(str(paths.database), read_only=True) as connection:
        result = connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()
    assert result is not None
    count = result[0]
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


def test_raw_snapshots_never_overwrite_when_timestamp_is_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> datetime:
            return datetime(2026, 9, 13, 7, 0, tzinfo=UTC)

    monkeypatch.setattr(storage, "datetime", FrozenDateTime)
    paths = get_paths(tmp_path)
    first = make_synthetic_daily_prices(periods=120)
    second = first.copy()
    second["amount"] = second["amount"] + 1

    first_path = save_raw_snapshot(first, paths)
    second_path = save_raw_snapshot(second, paths)

    assert first_path != second_path
    assert len(list(first_path.parent.glob("*.parquet"))) == 2
    pd.testing.assert_frame_equal(pd.read_parquet(first_path), first)
    pd.testing.assert_frame_equal(pd.read_parquet(second_path), second)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("symbol", r"C:\escape"),
        ("symbol", "../escape"),
        ("source", r"..\escape"),
        ("source", "source/child"),
    ],
)
def test_raw_snapshot_rejects_unsafe_path_components(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120)
    frame[column] = value

    with pytest.raises(ValueError, match="路径"):
        save_raw_snapshot(frame, paths)


def test_curated_write_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    original = make_synthetic_daily_prices(periods=120)
    output = save_curated(original, paths)
    expected_bytes = output.read_bytes()

    def interrupted_to_parquet(
        self: pd.DataFrame,
        path: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        Path(str(path)).write_bytes(b"partial parquet")
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", interrupted_to_parquet)

    with pytest.raises(OSError, match="simulated"):
        save_curated(original.tail(2).copy(), paths)

    assert output.read_bytes() == expected_bytes
    assert not list(output.parent.glob(".*.tmp*"))
