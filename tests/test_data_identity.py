from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from finance_lab import pipeline, storage
from finance_lab.config import Instrument, get_paths
from finance_lab.data_manifest import generate_dataset_manifest
from finance_lab.pipeline import validate_curated_data
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import read_curated, save_curated, sync_duckdb


@pytest.mark.parametrize("writer", ["save_raw_snapshot", "save_curated"])
@pytest.mark.parametrize(
    "symbol",
    [
        "SH.510300",
        "sh_510300",
        "sh.510300 ",
        " sh.510300",
        "sh.５１０３００",
        "sh..510300",
        "sh.51030",
        "sh.5103000",
        "sh.510300\n",
        "sh/510300",
        "s_h.510300",
        "命名.510300",
        "abcdefghijklmnopq.510300",
        "",
        None,
        510300,
        True,
    ],
)
def test_writers_reject_noncanonical_symbol_without_publishing(
    tmp_path: Path,
    writer: str,
    symbol: object,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=120)
    prices["symbol"] = symbol

    with pytest.raises(ValueError):
        getattr(storage, writer)(prices, paths)

    assert not list(paths.raw.rglob("*.parquet"))
    assert not list(paths.curated.iterdir())


@pytest.mark.parametrize("writer", ["save_raw_snapshot", "save_curated"])
def test_writers_reject_mixed_symbols_without_publishing(tmp_path: Path, writer: str) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=120)
    prices.loc[60:, "symbol"] = "demo.999999"

    with pytest.raises(ValueError, match="标的"):
        getattr(storage, writer)(prices, paths)

    assert not list(paths.raw.rglob("*.parquet"))
    assert not list(paths.curated.iterdir())


@pytest.mark.parametrize(
    "symbol", ["demo_000300", "DEMO.000300", "demo.000300.", "demo.０００３００"]
)
def test_read_rejects_noncanonical_request(tmp_path: Path, symbol: str) -> None:
    paths = get_paths(tmp_path)
    target = save_curated(make_synthetic_daily_prices(periods=120), paths)
    before = target.read_bytes()

    with pytest.raises(ValueError, match="标的"):
        read_curated(symbol, paths)

    assert target.read_bytes() == before


@pytest.mark.parametrize("filename", ["DEMO_000300.parquet", "demo_000300.PARQUET"])
def test_read_does_not_accept_noncanonical_existing_filename(
    tmp_path: Path,
    filename: str,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=120)
    target = paths.curated / filename
    prices.to_parquet(target, index=False)
    before = target.read_bytes()

    with pytest.raises((ValueError, FileNotFoundError)):
        read_curated("demo.000300", paths)

    assert target.read_bytes() == before
    assert list(paths.curated.iterdir()) == [target]


def _bad_identity_frame(mode: str) -> pd.DataFrame:
    prices = make_synthetic_daily_prices(periods=120)
    if mode == "foreign":
        prices["symbol"] = "demo.999999"
    elif mode == "mixed":
        prices.loc[60:, "symbol"] = "demo.999999"
    elif mode == "null":
        prices.loc[50, "symbol"] = None
    elif mode == "missing":
        prices = prices.drop(columns="symbol")
    elif mode == "empty":
        prices = prices.head(0)
    elif mode == "alias":
        prices["symbol"] = "demo_000300"
    else:
        assert mode == "valid"
    return prices


@pytest.mark.parametrize("mode", ["foreign", "mixed", "null", "missing", "empty", "alias"])
def test_read_refuses_invalid_file_identity(tmp_path: Path, mode: str) -> None:
    paths = get_paths(tmp_path)
    target = paths.curated / "demo_000300.parquet"
    _bad_identity_frame(mode).to_parquet(target, index=False)
    before = target.read_bytes()

    with pytest.raises(ValueError, match="标的"):
        read_curated("demo.000300", paths)

    assert target.read_bytes() == before


@pytest.mark.parametrize("mode", ["foreign", "mixed", "null", "missing", "empty", "alias"])
def test_merge_refuses_invalid_old_identity_without_overwriting(tmp_path: Path, mode: str) -> None:
    paths = get_paths(tmp_path)
    target = paths.curated / "demo_000300.parquet"
    _bad_identity_frame(mode).to_parquet(target, index=False)
    before = target.read_bytes()

    with pytest.raises(ValueError, match="标的"):
        save_curated(make_synthetic_daily_prices(periods=120), paths)

    assert target.read_bytes() == before
    assert list(paths.curated.iterdir()) == [target]


@pytest.mark.parametrize("mode", ["foreign", "mixed", "null", "missing", "empty", "alias"])
def test_sync_rejects_bad_identity_and_preserves_previous_table(tmp_path: Path, mode: str) -> None:
    paths = get_paths(tmp_path)
    target = save_curated(make_synthetic_daily_prices(periods=120), paths)
    assert sync_duckdb(paths) == 120
    _bad_identity_frame(mode).to_parquet(target, index=False)
    before = target.read_bytes()

    error_type = duckdb.BinderException if mode == "missing" else ValueError
    with pytest.raises(error_type):
        sync_duckdb(paths)

    with duckdb.connect(str(paths.database), read_only=True) as connection:
        assert connection.execute(
            "SELECT symbol, COUNT(*) FROM daily_prices GROUP BY symbol"
        ).fetchall() == [("demo.000300", 120)]
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    ("mode", "filename", "code"),
    [
        ("valid", "renamed.parquet", "symbol_filename_mismatch"),
        ("valid", "DEMO_000300.parquet", "symbol_filename_mismatch"),
        ("mixed", "demo_000300.parquet", "mixed_symbols"),
        ("null", "demo_000300.parquet", "invalid_symbol"),
        ("alias", "demo_000300.parquet", "invalid_symbol"),
        ("missing", "demo_000300.parquet", "missing_symbol"),
        ("empty", "demo_000300.parquet", "missing_symbol"),
    ],
)
def test_health_reports_structured_identity_errors(
    tmp_path: Path,
    mode: str,
    filename: str,
    code: str,
) -> None:
    paths = get_paths(tmp_path)
    target = paths.curated / filename
    _bad_identity_frame(mode).to_parquet(target, index=False)
    before = target.read_bytes()

    manifest = generate_dataset_manifest(paths, as_of_date=date(2026, 9, 18))
    assert manifest.health_status == "error"
    assert code in manifest.files[0].error_codes
    report = validate_curated_data(tmp_path)
    assert report["error_count"] >= 1
    assert code in {issue["code"] for issue in report["files"][0]["issues"]}
    assert target.read_bytes() == before


@pytest.mark.parametrize("symbol", ["sh.510300", "sz.000001", "demo.000300"])
def test_canonical_identity_round_trip_remains_compatible(tmp_path: Path, symbol: str) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=120)
    prices["symbol"] = symbol
    target = save_curated(prices, paths)

    assert target.name == symbol.replace(".", "_") + ".parquet"
    pd.testing.assert_frame_equal(read_curated(symbol, paths), prices)
    assert sync_duckdb(paths) == 120
    manifest = generate_dataset_manifest(paths, as_of_date=prices["trade_date"].max().date())
    assert manifest.files[0].error_codes == ()
    assert validate_curated_data(tmp_path)["error_count"] == 0


def test_sync_allows_multiple_separate_canonical_files(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    for symbol in ("sh.510300", "sh.000300"):
        prices = make_synthetic_daily_prices(periods=120)
        prices["symbol"] = symbol
        save_curated(prices, paths)

    assert sync_duckdb(paths) == 240
    with duckdb.connect(str(paths.database), read_only=True) as connection:
        assert connection.execute(
            "SELECT symbol, COUNT(*) FROM daily_prices GROUP BY symbol ORDER BY symbol"
        ).fetchall() == [("sh.000300", 120), ("sh.510300", 120)]


@pytest.mark.parametrize("fallback_valid", [False, True])
def test_fetch_verifies_requested_identity_before_saving_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fallback_valid: bool,
) -> None:
    paths = get_paths(tmp_path)
    wrong = make_synthetic_daily_prices(periods=120)
    wrong["symbol"] = "sh.510300"
    fallback = wrong.copy()
    if fallback_valid:
        fallback["symbol"] = "sh.000300"
    monkeypatch.setattr(pipeline, "fetch_akshare", lambda *args: wrong)
    monkeypatch.setattr(pipeline, "fetch_baostock", lambda *args: fallback)

    record = pipeline._fetch_instrument(
        Instrument(code="000300", exchange="sh", name="synthetic test", kind="index"),
        date(2022, 1, 1),
        date(2023, 1, 1),
        paths,
    )

    assert "akshare" in record.source_errors
    assert "标的" in record.source_errors["akshare"]
    assert len(list(paths.raw.rglob("*.parquet"))) == int(fallback_valid)
    assert len(list(paths.curated.glob("*.parquet"))) == int(fallback_valid)
    if fallback_valid:
        assert record.primary_source == "baostock"
        assert record.rows == 120
        assert set(read_curated("sh.000300", paths)["symbol"]) == {"sh.000300"}
    else:
        assert "baostock" in record.source_errors
        assert record.rows == 0
