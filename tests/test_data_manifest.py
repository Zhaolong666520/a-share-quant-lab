from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import finance_lab.data_manifest as manifest_module
from finance_lab.cli import build_parser
from finance_lab.config import get_paths
from finance_lab.data_manifest import (
    generate_dataset_manifest,
    read_update_summary,
    write_dataset_manifest_report,
)
from finance_lab.pipeline import data_health
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def test_manifest_is_stable_and_contains_file_lineage(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)
    last_date = prices["trade_date"].max().date()

    first = generate_dataset_manifest(paths, as_of_date=last_date)
    second = generate_dataset_manifest(paths, as_of_date=last_date)

    assert first.dataset_id == second.dataset_id
    assert len(first.dataset_id) == 64
    assert first.total_rows == 140
    assert len(first.files) == 1
    item = first.files[0]
    assert item.symbol == "demo.000300"
    assert item.rows == 140
    assert len(item.sha256) == 64
    assert item.start_date == prices["trade_date"].min().date()
    assert item.end_date == last_date
    assert item.sources == {"SYNTHETIC_DEMO_NOT_MARKET_DATA": 140}
    assert item.adjustments == ("none",)


def test_manifest_id_changes_when_curated_content_changes(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    output = save_curated(prices, paths)
    first = generate_dataset_manifest(paths, as_of_date=prices["trade_date"].max().date())

    changed = prices.copy()
    changed.loc[10, "close"] = float(changed.loc[10, "close"]) + 0.01
    changed.to_parquet(output, index=False)
    second = generate_dataset_manifest(paths, as_of_date=prices["trade_date"].max().date())

    assert first.dataset_id != second.dataset_id
    assert first.files[0].sha256 != second.files[0].sha256


def test_manifest_warns_about_staleness_mixed_sources_and_unadjusted_data(
    tmp_path: Path,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    prices.loc[40:, "source"] = "SECOND_SOURCE"
    save_curated(prices, paths)
    last_date = prices["trade_date"].max().date()

    manifest = generate_dataset_manifest(
        paths,
        as_of_date=last_date + timedelta(days=10),
        stale_after_business_days=2,
    )

    item = manifest.files[0]
    assert item.business_days_stale is not None
    assert item.business_days_stale > 2
    assert "stale_data" in item.warning_codes
    assert "mixed_sources" in item.warning_codes
    assert "unadjusted_prices" in item.warning_codes
    assert manifest.health_status == "warning"


def test_manifest_rejects_future_data_relative_to_as_of_date(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)

    manifest = generate_dataset_manifest(
        paths,
        as_of_date=prices["trade_date"].max().date() - timedelta(days=1),
    )

    assert "data_after_as_of" in manifest.files[0].error_codes
    assert manifest.health_status == "error"


def test_manifest_surfaces_latest_upstream_source_errors(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)
    summary = {
        "end": "2026-08-21",
        "records": [
            {
                "symbol": "demo.000300",
                "primary_source": "baostock",
                "source_errors": {"akshare": "temporary upstream failure"},
            }
        ],
    }
    (paths.outputs / "update_summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )

    manifest = generate_dataset_manifest(
        paths,
        as_of_date=prices["trade_date"].max().date(),
    )

    assert manifest.upstream_errors == {
        "demo.000300": {"akshare": "temporary upstream failure"}
    }
    assert manifest.health_status == "warning"


def test_read_update_summary_preserves_successful_fallback_and_source_error(
    tmp_path: Path,
) -> None:
    paths = get_paths(tmp_path)
    (paths.outputs / "update_summary.json").write_text(
        json.dumps(
            {
                "end": "2026-08-21",
                "records": [
                    {
                        "symbol": "sh.510300",
                        "rows": 3,
                        "curated_file": "data/curated/sh_510300.parquet",
                        "source_errors": {"akshare": "temporary upstream failure"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = read_update_summary(paths)

    assert summary.parse_error is None
    assert summary.end == "2026-08-21"
    assert summary.records[0].symbol == "sh.510300"
    assert summary.records[0].rows == 3
    assert summary.records[0].curated_file == "data/curated/sh_510300.parquet"
    assert summary.records[0].source_errors == {"akshare": "temporary upstream failure"}


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "[]",
        json.dumps({"records": {}}),
        json.dumps({"records": [{"symbol": "sh.510300", "rows": 1, "source_errors": []}]}),
        json.dumps({"records": [{"rows": 1}]}),
    ],
)
def test_read_update_summary_degrades_malformed_content_without_crashing(
    tmp_path: Path,
    payload: str,
) -> None:
    paths = get_paths(tmp_path)
    (paths.outputs / "update_summary.json").write_text(payload, encoding="utf-8")

    summary = read_update_summary(paths)

    assert summary.records == ()
    assert summary.parse_error is not None


def test_read_update_summary_handles_missing_file_explicitly(tmp_path: Path) -> None:
    summary = read_update_summary(get_paths(tmp_path))

    assert summary.end is None
    assert summary.records == ()


def test_manifest_degrades_malformed_update_summary_to_warning(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)
    (paths.outputs / "update_summary.json").write_text("[]", encoding="utf-8")

    manifest = generate_dataset_manifest(
        paths,
        as_of_date=prices["trade_date"].max().date(),
    )

    assert "__update_summary__" in manifest.upstream_errors
    assert manifest.health_status == "warning"


def test_manifest_structures_missing_columns_and_provenance_errors(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)
    bad = prices.drop(columns=["trade_date", "source"]).copy()
    bad["adjustment"] = None
    bad["volume_unit"] = ""
    bad["ingested_at"] = None
    bad.to_parquet(paths.curated / "bad.parquet", index=False)

    manifest = generate_dataset_manifest(paths, as_of_date=date(2026, 8, 24))
    item = next(entry for entry in manifest.files if entry.relative_path.endswith("bad.parquet"))

    assert manifest.health_status == "error"
    assert item.start_date is None
    assert item.end_date is None
    assert item.business_days_stale is None
    assert {
        "missing_columns",
        "missing_source",
        "missing_adjustment",
        "missing_volume_unit",
        "missing_ingested_at",
    }.issubset(item.error_codes)


def test_manifest_rejects_mixed_symbols_and_provenance_units(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    prices.loc[1, "symbol"] = "demo.999999"
    prices.loc[1, "adjustment"] = "qfq"
    prices.loc[1, "volume_unit"] = "share"
    prices.to_parquet(paths.curated / "mixed.parquet", index=False)

    manifest = generate_dataset_manifest(
        paths,
        as_of_date=prices["trade_date"].max().date(),
    )
    item = manifest.files[0]

    assert {"mixed_symbols", "mixed_adjustments", "mixed_volume_units"}.issubset(
        item.error_codes
    )


def test_manifest_detects_file_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)
    hashes = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(manifest_module, "file_sha256", lambda _path: next(hashes))

    with pytest.raises(RuntimeError, match="发生变化"):
        generate_dataset_manifest(
            paths,
            as_of_date=prices["trade_date"].max().date(),
        )


def test_manifest_report_writes_json_and_html(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)
    manifest = generate_dataset_manifest(
        paths,
        as_of_date=prices["trade_date"].max().date(),
    )

    json_path, html_path = write_dataset_manifest_report(manifest, paths)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["dataset_id"] == manifest.dataset_id
    assert payload["total_rows"] == 140
    text = html_path.read_text(encoding="utf-8")
    assert "数据集健康报告" in text
    assert "不复权" in text
    assert manifest.dataset_id in text


def test_manifest_report_paths_distinguish_freshness_context(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)
    last_date = prices["trade_date"].max().date()
    first = generate_dataset_manifest(paths, as_of_date=last_date)
    second = generate_dataset_manifest(paths, as_of_date=last_date + timedelta(days=10))

    first_paths = write_dataset_manifest_report(first, paths)
    second_paths = write_dataset_manifest_report(second, paths)

    assert first.dataset_id == second.dataset_id
    assert first_paths != second_paths


@pytest.mark.parametrize("stale_after_business_days", [-1, 1.5, True])
def test_manifest_rejects_invalid_stale_threshold(
    tmp_path: Path,
    stale_after_business_days: int,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)

    with pytest.raises(ValueError):
        generate_dataset_manifest(
            paths,
            as_of_date=date(2026, 1, 1),
            stale_after_business_days=stale_after_business_days,
        )


def test_manifest_requires_curated_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="curated"):
        generate_dataset_manifest(get_paths(tmp_path), as_of_date=date(2026, 1, 1))


def test_data_health_pipeline_writes_report(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    save_curated(prices, paths)

    report, payload = data_health(
        root=tmp_path,
        as_of_date=prices["trade_date"].max().date(),
    )

    assert report.exists()
    assert payload["dataset_id"]
    assert payload["total_rows"] == 140


def test_manifest_cli_accepts_freshness_settings() -> None:
    args = build_parser().parse_args(
        ["manifest", "--as-of", "2026-08-24", "--stale-after-business-days", "4"]
    )

    assert args.as_of == date(2026, 8, 24)
    assert args.stale_after_business_days == 4
