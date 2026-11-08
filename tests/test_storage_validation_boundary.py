"""Synthetic-only rejection and rollback checks at durable write boundaries."""

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from finance_lab import storage
from finance_lab.config import get_paths
from finance_lab.paper_store import PaperStore
from finance_lab.sample import make_synthetic_daily_prices
from tests.test_paper_store import _accounts_and_initial_steps

BAD_CASES = [
    "empty_source",
    "null_source",
    "empty_unit",
    "mixed_unit",
    "empty_adjustment",
    "adjusted",
    "bad_time",
    "null_time",
    "numeric_time",
    "boolean_time",
    "negative_price",
    "duplicate_date",
    "missing_close",
    "missing_unit",
]


def bad_frame(frame, case):
    frame = frame.copy()
    replacements = {
        "empty_source": ("source", " "),
        "null_source": ("source", None),
        "empty_unit": ("volume_unit", ""),
        "mixed_unit": ("volume_unit", "lot"),
        "empty_adjustment": ("adjustment", ""),
        "adjusted": ("adjustment", "qfq"),
        "bad_time": ("ingested_at", "not-a-time"),
        "null_time": ("ingested_at", None),
        "negative_price": ("close", -1.0),
        "mixed_source": ("source", "SECOND_SOURCE"),
    }
    if case in replacements:
        column, value = replacements[case]
        frame.loc[frame.index[1], column] = value
    elif case in {"numeric_time", "boolean_time"}:
        frame["ingested_at"] = 1 if case == "numeric_time" else True
    elif case == "duplicate_date":
        frame = pd.concat([frame.iloc[[0]], frame], ignore_index=True)
    else:
        frame = frame.drop(columns="close" if case == "missing_close" else "volume_unit")
    return frame


def files(root: Path):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def database_state(database):
    with duckdb.connect(str(database), read_only=True) as connection:
        names = [row[0] for row in connection.execute("SHOW TABLES").fetchall()]
        return {
            name: (
                connection.execute(f'DESCRIBE "{name}"').fetchall(),
                connection.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall(),
            )
            for name in names
        }


@pytest.fixture
def populated(tmp_path):
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120)
    storage.save_curated(frame, paths)
    assert storage.sync_duckdb(paths) == len(frame)
    a, b, first, second, context = _accounts_and_initial_steps()
    with PaperStore(paths.database) as store:
        store.ensure_schema()
        store.initialize_portfolio((a, b), (first, second), context, batch_id="synthetic")
    return paths, frame


@pytest.mark.parametrize("writer", ["save_raw_snapshot", "save_curated"])
@pytest.mark.parametrize("case", BAD_CASES)
def test_invalid_input_keeps_published_files(tmp_path, writer, case):
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120)
    storage.save_raw_snapshot(frame, paths)
    storage.save_curated(frame, paths)
    before = files(paths.raw), files(paths.curated)
    failure = None
    try:
        getattr(storage, writer)(bad_frame(frame, case), paths)
    except Exception as exc:
        failure = exc
    assert (files(paths.raw), files(paths.curated)) == before
    assert isinstance(failure, ValueError), repr(failure)


def test_raw_rejects_mixed_source_without_publishing(tmp_path):
    paths = get_paths(tmp_path)
    frame = bad_frame(make_synthetic_daily_prices(periods=120), "mixed_source")
    with pytest.raises(ValueError):
        storage.save_raw_snapshot(frame, paths)
    assert not files(paths.raw)


@pytest.mark.parametrize("case", BAD_CASES)
def test_bad_second_file_keeps_market_and_nonempty_ledger(populated, case):
    paths, frame = populated
    before = database_state(paths.database)
    valid = frame.copy()
    valid["amount"] += 1
    valid.to_parquet(paths.curated / "demo_000300.parquet", index=False)
    bad = bad_frame(frame, case)
    bad["symbol"] = "demo.999999"
    bad.to_parquet(paths.curated / "demo_999999.parquet", index=False)
    before_files = files(paths.curated)
    failure = None
    try:
        storage.sync_duckdb(paths)
    except Exception as exc:
        failure = exc
    assert database_state(paths.database) == before
    assert files(paths.curated) == before_files
    assert isinstance(failure, ValueError), repr(failure)


def test_bad_file_does_not_create_a_database(tmp_path):
    paths = get_paths(tmp_path)
    bad = bad_frame(make_synthetic_daily_prices(periods=120), "negative_price")
    bad.to_parquet(paths.curated / "demo_000300.parquet", index=False)
    with pytest.raises(ValueError):
        storage.sync_duckdb(paths)
    assert not paths.database.exists()


@pytest.mark.parametrize("case", ["bad_time", "negative_price", "duplicate_date"])
def test_bad_previous_curated_is_not_silently_replaced(tmp_path, case):
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120)
    target = paths.curated / "demo_000300.parquet"
    bad_frame(frame, case).to_parquet(target, index=False)
    before = files(paths.curated)
    failure = None
    try:
        storage.save_curated(frame, paths)
    except Exception as exc:
        failure = exc
    assert files(paths.curated) == before
    assert isinstance(failure, ValueError), repr(failure)


@pytest.mark.parametrize("unit", ["share", "shares", "lot", "synthetic_unit"])
def test_legal_metadata_and_mixed_curated_sources_still_work(tmp_path, unit):
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120)
    frame["volume_unit"] = unit
    frame["ingested_at"] = [
        "2026-11-06T00:00:00Z",
        "2026-11-06T00:00:00+00:00",
        "2026-11-06T08:00:00+08:00",
        "2026-11-06T00:00:00",
    ] * 30
    raw = storage.save_raw_snapshot(frame, paths)
    pd.testing.assert_frame_equal(pd.read_parquet(raw), frame)
    frame.loc[1, "source"] = "SECOND_SOURCE"
    target = storage.save_curated(frame, paths)
    assert storage.sync_duckdb(paths) == len(frame)
    pd.testing.assert_frame_equal(pd.read_parquet(target), frame)


@pytest.mark.parametrize("overlap", [True, False])
def test_unit_change_respects_final_source_selection(tmp_path, overlap):
    paths = get_paths(tmp_path)
    old = make_synthetic_daily_prices(periods=120)
    old["source"], old["volume_unit"] = "akshare", "lot"
    target = storage.save_curated(old, paths)
    incoming = old.copy()
    incoming["source"], incoming["volume_unit"] = "tencent", "share"
    if not overlap:
        incoming["trade_date"] += pd.Timedelta(days=365)
    raw = storage.save_raw_snapshot(incoming, paths)
    raw_before, before = files(paths.raw), files(paths.curated)
    if overlap:
        storage.save_curated(incoming, paths, preferred_source="tencent")
        assert pd.read_parquet(target)["volume_unit"].eq("share").all()
    else:
        with pytest.raises(ValueError):
            storage.save_curated(incoming, paths, preferred_source="tencent")
        assert files(paths.curated) == before
    assert raw.exists() and files(paths.raw) == raw_before


def test_post_replacement_count_failure_rolls_back_market_and_ledger(populated, monkeypatch):
    paths, frame = populated
    before = database_state(paths.database)
    frame["amount"] += 10
    frame.to_parquet(paths.curated / "demo_000300.parquet", index=False)
    connect = duckdb.connect
    observed = []

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.connection.close()

        def execute(self, query, *args, **kwargs):
            if "SELECT COUNT(*) FROM daily_prices" in query:
                observed.append("count_failure")
                raise RuntimeError("injected count failure after replacement")
            result = self.connection.execute(query, *args, **kwargs)
            if "CREATE OR REPLACE TABLE daily_prices" in query:
                observed.append("replacement_executed")
            return result

    def injected(database=":memory:", *args, **kwargs):
        connection = connect(database, *args, **kwargs)
        return FailingConnection(connection) if str(database) == str(paths.database) else connection

    with monkeypatch.context() as patch:
        patch.setattr(storage.duckdb, "connect", injected)
        with pytest.raises(RuntimeError, match="injected count"):
            storage.sync_duckdb(paths)
    assert observed == ["replacement_executed", "count_failure"]
    assert database_state(paths.database) == before
