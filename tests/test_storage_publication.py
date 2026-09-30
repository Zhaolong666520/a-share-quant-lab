from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pytest

from finance_lab import atomic_io
from finance_lab.config import get_paths
from finance_lab.data_manifest import generate_dataset_manifest
from finance_lab.pipeline import validate_curated_data
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated, sync_duckdb


def _read_rows(root: Path, reader: str) -> int:
    paths = get_paths(root)
    if reader == "database":
        return sync_duckdb(paths)
    if reader == "manifest":
        manifest = generate_dataset_manifest(paths)
        assert len(manifest.files) == 1
        return manifest.total_rows
    assert reader == "validation"
    report = validate_curated_data(root)
    assert len(report["files"]) == 1
    assert report["error_count"] == 0
    return int(report["files"][0]["rows"])


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
@pytest.mark.parametrize("complete", [False, True], ids=["partial", "complete-unpublished"])
def test_readers_ignore_in_progress_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
    complete: bool,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=150)
    save_curated(prices.head(120).copy(), paths)
    to_parquet = pd.DataFrame.to_parquet

    def paused_write(self: pd.DataFrame, path: object, **kwargs: object) -> None:
        temporary = Path(str(path))
        if complete:
            to_parquet(self, temporary, index=False)
        else:
            temporary.write_bytes(b"partial parquet")
        assert _read_rows(tmp_path, reader) == 120
        if not complete:
            to_parquet(self, temporary, index=False)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", paused_write)
    save_curated(prices, paths)
    assert _read_rows(tmp_path, reader) == 150
    assert not list(paths.curated.glob(".*.tmp*"))


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
@pytest.mark.parametrize("complete", [False, True], ids=["partial", "complete-unpublished"])
def test_readers_ignore_legacy_temporary_file(
    tmp_path: Path,
    reader: str,
    complete: bool,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=120)
    save_curated(prices, paths)
    temporary = paths.curated / ".demo_000300.parquet.0123456789abcdef.tmp.parquet"
    if complete:
        prices.to_parquet(temporary, index=False)
    else:
        temporary.write_bytes(b"partial parquet")
    before = temporary.read_bytes()
    assert _read_rows(tmp_path, reader) == 120
    assert temporary.read_bytes() == before


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
def test_corrupted_published_file_is_not_silently_skipped(tmp_path: Path, reader: str) -> None:
    paths = get_paths(tmp_path)
    save_curated(make_synthetic_daily_prices(periods=120), paths)
    (paths.curated / "broken.parquet").write_bytes(b"not parquet")
    if reader == "validation":
        report = validate_curated_data(tmp_path)
        assert report["status"] == "failed"
        assert report["error_count"] >= 1
        assert "unreadable_parquet" in {
            issue["code"] for item in report["files"] for issue in item["issues"]
        }
    else:
        error_type = duckdb.InvalidInputException if reader == "database" else pa.ArrowInvalid
        with pytest.raises(error_type):
            _read_rows(tmp_path, reader)


@pytest.mark.parametrize("writer_name", ["write_parquet_atomic", "write_parquet_immutable"])
def test_staged_parquet_has_no_published_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_name: str,
) -> None:
    prices = make_synthetic_daily_prices(periods=120)
    to_parquet = pd.DataFrame.to_parquet

    def paused_write(self: pd.DataFrame, path: object, **kwargs: object) -> None:
        to_parquet(self, Path(str(path)), index=False)
        assert list(tmp_path.glob("*.parquet")) == []

    monkeypatch.setattr(pd.DataFrame, "to_parquet", paused_write)
    target = tmp_path / "prices.parquet"
    getattr(atomic_io, writer_name)(prices, target)
    pd.testing.assert_frame_equal(pd.read_parquet(target), prices)
    assert list(tmp_path.iterdir()) == [target]


def test_replace_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=150)
    target = save_curated(prices.head(120).copy(), paths)
    before = target.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated publish failure"):
        save_curated(prices, paths)
    assert target.read_bytes() == before
    assert list(paths.curated.iterdir()) == [target]


def test_immutable_collision_preserves_original(tmp_path: Path) -> None:
    target = tmp_path / "raw.parquet"
    prices = make_synthetic_daily_prices(periods=120)
    atomic_io.write_parquet_immutable(prices, target)
    before = target.read_bytes()
    with pytest.raises(RuntimeError, match="不可覆盖"):
        atomic_io.write_parquet_immutable(prices.tail(2), target)
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_published_enumeration_only_excludes_reserved_staging(tmp_path: Path) -> None:
    names = [
        "z.parquet",
        "a.parquet",
        ".hidden.parquet",
        "notes.tmp.parquet",
        ".prices.parquet.deadbeef.tmp.parquet",
        ".prices.parquet.deadbeef.tmp",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"candidate")
    assert [path.name for path in atomic_io.published_parquet_files(tmp_path)] == [
        ".hidden.parquet",
        "a.parquet",
        "notes.tmp.parquet",
        "z.parquet",
    ]


@pytest.mark.parametrize("reader", ["database", "manifest", "validation"])
def test_only_legacy_staging_is_treated_as_empty(tmp_path: Path, reader: str) -> None:
    paths = get_paths(tmp_path)
    temporary = paths.curated / ".prices.parquet.deadbeef.tmp.parquet"
    temporary.write_bytes(b"partial parquet")
    if reader == "database":
        assert sync_duckdb(paths) == 0
    elif reader == "manifest":
        with pytest.raises(FileNotFoundError, match="没有"):
            generate_dataset_manifest(paths)
    else:
        assert validate_curated_data(tmp_path)["files"] == []
    assert temporary.read_bytes() == b"partial parquet"
