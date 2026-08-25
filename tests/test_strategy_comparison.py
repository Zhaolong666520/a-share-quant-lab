from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance_lab.cli import build_parser
from finance_lab.config import get_paths
from finance_lab.pipeline import strategy_compare_symbol
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated
from finance_lab.strategy_comparison import STRATEGY_ORDER, run_strategy_comparison
from finance_lab.strategy_comparison_report import write_strategy_comparison_report


def test_comparison_keeps_fixed_order_boundaries_cost_and_benchmark() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_strategy_comparison(prices, split_date=date(2024, 1, 1))

    assert tuple(scenario.strategy for scenario in result.scenarios) == STRATEGY_ORDER
    reference = result.scenarios[0].out_of_sample_frame
    assert (reference["trade_date"].dt.date >= result.split_date).all()
    full_frame = result.scenarios[0].full_result.frame
    cross_boundary = full_frame[
        (full_frame["trade_date"].dt.date < result.split_date)
        & (full_frame["return_end_date"].dt.date >= result.split_date)
    ]
    assert len(cross_boundary) == 1
    assert not reference["trade_date"].isin(cross_boundary["trade_date"]).any()
    for scenario in result.scenarios:
        frame = scenario.out_of_sample_frame
        assert scenario.full_result.cost_bps == 5.0
        assert frame["return_end_date"].reset_index(drop=True).equals(
            reference["return_end_date"].reset_index(drop=True)
        )
        pd.testing.assert_series_equal(
            frame["benchmark_return"].reset_index(drop=True),
            reference["benchmark_return"].reset_index(drop=True),
        )
        assert scenario.out_of_sample_metrics.observations == len(reference)
    assert result.scenarios[0].out_of_sample_metrics.to_dict() == (
        result.benchmark_metrics.to_dict()
    )


def test_comparison_validates_indicator_warmup_at_first_oos_trade() -> None:
    prices = make_synthetic_daily_prices(periods=400)
    split_date = pd.Timestamp(prices.iloc[120]["trade_date"]).date()

    with pytest.raises(ValueError, match="时间序列动量.*预热"):
        run_strategy_comparison(
            prices,
            split_date=split_date,
            momentum_lookback=120,
            min_period_observations=2,
        )


def test_comparison_id_changes_with_momentum_configuration() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        momentum_lookback=120,
    )
    second = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        momentum_lookback=121,
    )

    assert first.experiment_id != second.experiment_id


def test_comparison_id_uses_curated_file_hash_when_available() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        curated_file_sha256="a" * 64,
    )
    second = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        curated_file_sha256="b" * 64,
    )

    assert first.experiment_id != second.experiment_id
    assert first.experiment_id.endswith("_" + "a" * 16)


def test_comparison_rejects_invalid_curated_file_hash() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="64位十六进制"):
        run_strategy_comparison(
            prices,
            split_date=date(2024, 1, 1),
            curated_file_sha256="../not-a-hash",
        )


def test_semantic_data_fingerprint_includes_provenance_and_volume() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    baseline = run_strategy_comparison(prices, split_date=date(2024, 1, 1))
    changed = prices.copy()
    changed.loc[0, "volume"] += 1
    changed.loc[0, "amount"] += 1
    changed.loc[0, "ingested_at"] = "2099-01-01T00:00:00+00:00"
    mutated = run_strategy_comparison(changed, split_date=date(2024, 1, 1))

    assert baseline.data_fingerprint != mutated.data_fingerprint
    assert baseline.experiment_id != mutated.experiment_id


def test_report_paths_preserve_different_health_contexts(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        manifest_as_of_date=date(2026, 8, 20),
        business_days_stale=4,
        data_health_status="warning",
    )
    second = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        manifest_as_of_date=date(2026, 8, 21),
        business_days_stale=5,
        data_health_status="warning",
    )

    first_outputs = write_strategy_comparison_report(first, get_paths(tmp_path))
    second_outputs = write_strategy_comparison_report(second, get_paths(tmp_path))
    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)


def test_report_context_includes_global_dataset_id(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        dataset_id="a" * 64,
    )
    second = run_strategy_comparison(
        prices,
        split_date=date(2024, 1, 1),
        dataset_id="b" * 64,
    )

    first_outputs = write_strategy_comparison_report(first, get_paths(tmp_path))
    second_outputs = write_strategy_comparison_report(second, get_paths(tmp_path))
    assert first_outputs[0] != second_outputs[0]


def test_report_symbol_cannot_escape_outputs_directory(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    prices["symbol"] = r"C:\temp\escape"
    result = run_strategy_comparison(prices, split_date=date(2024, 1, 1))
    outputs = write_strategy_comparison_report(result, get_paths(tmp_path))

    output_root = (tmp_path / "outputs").resolve()
    assert all(path.resolve().is_relative_to(output_root) for path in outputs)


def test_future_prices_do_not_change_development_metrics() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    split_date = date(2024, 1, 1)
    baseline = run_strategy_comparison(prices, split_date=split_date)
    changed = prices.copy()
    future_mask = changed["trade_date"].dt.date >= split_date
    changed.loc[future_mask, ["open", "high", "low", "close"]] *= 1.4
    mutated = run_strategy_comparison(changed, split_date=split_date)

    for baseline_scenario, mutated_scenario in zip(
        baseline.scenarios, mutated.scenarios, strict=True
    ):
        assert baseline_scenario.development_metrics.to_dict() == (
            mutated_scenario.development_metrics.to_dict()
        )


def test_comparison_report_preserves_all_strategies_without_winner(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_strategy_comparison(prices, split_date=date(2024, 1, 1))
    outputs = write_strategy_comparison_report(result, get_paths(tmp_path))

    assert all(path.exists() for path in outputs)
    document = outputs[0].read_text(encoding="utf-8")
    assert "不会根据" in document
    assert "推荐" in document
    payload = json.loads(outputs[2].read_text(encoding="utf-8"))
    assert [item["strategy"] for item in payload["scenarios"]] == list(STRATEGY_ORDER)
    summary = pd.read_csv(outputs[3])
    assert summary["strategy"].tolist() == list(STRATEGY_ORDER)


def test_comparison_pipeline_binds_manifest_and_writes_report(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    paths = get_paths(tmp_path)
    save_curated(prices, paths)
    (paths.outputs / "update_summary.json").write_text(
        json.dumps(
            {
                "end": "2026-08-25",
                "records": [
                    {
                        "symbol": "demo.000300",
                        "source_errors": {"akshare": "<script>upstream failed</script>"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report, payload = strategy_compare_symbol(
        "demo.000300",
        split_date=date(2024, 1, 1),
        root=tmp_path,
    )

    assert report.exists()
    settings = payload["settings"]
    assert isinstance(settings, dict)
    assert settings["dataset_id"]
    assert settings["curated_file_sha256"]
    assert settings["manifest_as_of_date"]
    assert settings["data_start_date"]
    assert settings["data_end_date"]
    assert settings["data_sources"]
    assert settings["upstream_errors"] == {
        "akshare": "<script>upstream failed</script>"
    }
    document = report.read_text(encoding="utf-8")
    assert "&lt;script&gt;upstream failed&lt;/script&gt;" in document
    assert "<script>upstream failed</script>" not in document
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list)
    assert [item["strategy"] for item in scenarios] == list(STRATEGY_ORDER)


def test_comparison_cli_accepts_fixed_strategy_settings() -> None:
    args = build_parser().parse_args(
        [
            "compare",
            "--symbol",
            "sh.000300",
            "--split-date",
            "2024-01-01",
            "--short",
            "15",
            "--long",
            "50",
            "--momentum-lookback",
            "100",
            "--cost-bps",
            "7.5",
        ]
    )

    assert args.command == "compare"
    assert args.short == 15
    assert args.long == 50
    assert args.momentum_lookback == 100
    assert args.cost_bps == 7.5
