from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from finance_lab.cli import build_parser
from finance_lab.config import get_paths
from finance_lab.parameter_sensitivity import run_parameter_sensitivity
from finance_lab.parameter_sensitivity_report import write_parameter_sensitivity_report
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.validation import DataValidationError


def test_parameter_grid_runs_every_pair_and_keeps_oos_boundaries_consistent() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        short_windows=(10, 20, 30),
        long_windows=(40, 60, 90),
        min_fold_observations=20,
    )

    assert [(scenario.short_window, scenario.long_window) for scenario in result.scenarios] == [
        (10, 40),
        (10, 60),
        (10, 90),
        (20, 40),
        (20, 60),
        (20, 90),
        (30, 40),
        (30, 60),
        (30, 90),
    ]
    reference = result.reference_scenario.walk_forward
    consistency_columns = ["trade_date", "return_end_date", "fold_index", "benchmark_return"]
    reference_boundaries = [
        (fold.train_start, fold.train_end, fold.oos_start, fold.oos_end)
        for fold in reference.folds
    ]
    for scenario in result.scenarios:
        candidate = scenario.walk_forward
        assert candidate.aggregate_frame[consistency_columns].equals(
            reference.aggregate_frame[consistency_columns]
        )
        assert [
            (fold.train_start, fold.train_end, fold.oos_start, fold.oos_end)
            for fold in candidate.folds
        ] == reference_boundaries
        assert candidate.aggregate_benchmark_metrics.to_dict() == (
            reference.aggregate_benchmark_metrics.to_dict()
        )
        assert candidate.execution_checks.passed


def test_parameter_summary_is_derived_from_scenarios() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=20,
    )

    returns = [scenario.metrics.total_return for scenario in result.scenarios]
    assert result.total_scenarios == 9
    assert result.reference_scenario.short_window == 20
    assert result.reference_scenario.long_window == 60
    assert result.profitable_scenarios == sum(value > 0 for value in returns)
    assert result.beats_benchmark_scenarios == sum(
        scenario.metrics.total_return > scenario.benchmark_metrics.total_return
        for scenario in result.scenarios
    )
    assert result.return_range == pytest.approx(max(returns) - min(returns))


@pytest.mark.parametrize(
    ("short_windows", "long_windows"),
    [
        ((10,), (40, 60)),
        ((20, 10), (40, 60)),
        ((10, 10), (40, 60)),
        ((0, 10), (40, 60)),
        ((10, 20.0), (40, 60)),
        (tuple(range(1, 10)), (40, 60)),
        ((10, 20), (40,)),
        ((10, 20), (60, 40)),
        ((10, 20), (40, 40)),
        ((10, 20), (0, 40)),
        ((10, 20), (40, 60.0)),
        ((10, 20), tuple(range(40, 49))),
        ((10, 50), (40, 60)),
    ],
)
def test_invalid_parameter_grids_are_rejected(
    short_windows: tuple[int, ...],
    long_windows: tuple[int, ...],
) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="windows"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            short_windows=short_windows,
            long_windows=long_windows,
            reference_pair=(10, 40),
            min_fold_observations=20,
        )


def test_reference_pair_must_exist_in_grid() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="reference_pair"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            short_windows=(10, 20),
            long_windows=(40, 60),
            reference_pair=(30, 90),
            min_fold_observations=20,
        )


def test_reference_pair_must_have_exactly_two_windows() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="reference_pair"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            reference_pair=(20,),  # type: ignore[arg-type]
            min_fold_observations=20,
        )


@pytest.mark.parametrize("cost_bps", [-1.0, float("nan"), float("inf")])
def test_invalid_parameter_test_cost_is_rejected(cost_bps: float) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="cost_bps"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            cost_bps=cost_bps,
            min_fold_observations=20,
        )


def test_cost_that_breaks_positive_equity_factor_is_rejected() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="净值因子"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            cost_bps=20_000.0,
            min_fold_observations=20,
        )


def test_non_finite_prices_are_rejected_before_parameter_comparison() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    prices.loc[5, ["close", "high"]] = float("inf")

    with pytest.raises(DataValidationError, match="有限"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            min_fold_observations=20,
        )


def test_grid_must_be_warmed_up_before_first_oos_window() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="预热"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            short_windows=(100, 200),
            long_windows=(300, 400),
            reference_pair=(100, 300),
            min_fold_observations=20,
        )


def test_warmup_requires_one_extra_row_for_next_day_execution() -> None:
    prices = make_synthetic_daily_prices(periods=200)
    short_windows = (5, 10)
    long_windows = (20, 30)
    with pytest.raises(ValueError, match="需要 31 行"):
        run_parameter_sensitivity(
            prices,
            first_oos_date=prices["trade_date"].iloc[30].date(),
            short_windows=short_windows,
            long_windows=long_windows,
            reference_pair=(10, 30),
            min_development_observations=2,
            min_fold_observations=2,
        )

    result = run_parameter_sensitivity(
        prices,
        first_oos_date=prices["trade_date"].iloc[31].date(),
        short_windows=short_windows,
        long_windows=long_windows,
        reference_pair=(10, 30),
        min_development_observations=2,
        min_fold_observations=2,
    )
    reference = result.reference_scenario.walk_forward
    first_oos_row = reference.aggregate_frame.iloc[0]
    signal_row = reference.full_result.frame.loc[
        reference.full_result.frame["trade_date"] == first_oos_row["signal_date"]
    ].iloc[0]

    assert math.isfinite(float(signal_row["sma_long"]))


def test_parameter_report_creates_html_json_chart_and_csv(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=20,
    )
    outputs = write_parameter_sensitivity_report(result, get_paths(tmp_path))

    assert all(path.exists() and path.stat().st_size > 0 for path in outputs)
    report_text = outputs[0].read_text(encoding="utf-8")
    assert "参数稳健性检验" in report_text
    assert "不是参数优化" in report_text
    assert "20/60" in report_text
    assert "个百分点" in report_text
    payload = json.loads(outputs[2].read_text(encoding="utf-8"))
    assert payload["settings"]["short_windows"] == [10, 20, 30]
    assert len(payload["scenarios"]) == 9
    csv_lines = outputs[3].read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_lines) == 10


def test_parameter_report_uses_the_configured_reference_pair(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        short_windows=(10, 20),
        long_windows=(40, 60),
        reference_pair=(10, 40),
        min_fold_observations=20,
    )
    outputs = write_parameter_sensitivity_report(result, get_paths(tmp_path))
    report_text = outputs[0].read_text(encoding="utf-8")

    assert "蓝框是预先指定的 10/40 参照组" in report_text
    assert "蓝框是预先指定的 20/60 参照组" not in report_text


def test_all_losing_scenarios_are_preserved_in_every_report_output(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        cost_bps=1000.0,
        min_fold_observations=20,
    )
    outputs = write_parameter_sensitivity_report(result, get_paths(tmp_path))

    assert result.profitable_scenarios == 0
    assert len(result.scenarios) == 9
    report_text = outputs[0].read_text(encoding="utf-8")
    assert "全部参数组合的聚合收益均为负" in report_text
    for scenario in result.scenarios:
        assert report_text.count(f"<td>{scenario.short_window}/{scenario.long_window}") == 1
    payload = json.loads(outputs[2].read_text(encoding="utf-8"))
    assert len(payload["scenarios"]) == 9
    assert len(outputs[3].read_text(encoding="utf-8-sig").splitlines()) == 10


def test_different_parameter_grids_preserve_both_reports(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        short_windows=(10, 20),
        long_windows=(40, 60),
        reference_pair=(20, 60),
        min_fold_observations=20,
    )
    second = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        short_windows=(10, 20, 30),
        long_windows=(40, 60),
        reference_pair=(20, 60),
        min_fold_observations=20,
    )
    first_outputs = write_parameter_sensitivity_report(first, get_paths(tmp_path))
    second_outputs = write_parameter_sensitivity_report(second, get_paths(tmp_path))

    assert first.experiment_id != second.experiment_id
    assert len(first.experiment_id) < 180
    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)


def test_high_precision_costs_have_distinct_parameter_experiment_ids() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        short_windows=(10, 20),
        long_windows=(40, 60),
        reference_pair=(20, 60),
        cost_bps=5.0000001,
        min_fold_observations=20,
    )
    second = run_parameter_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        short_windows=(10, 20),
        long_windows=(40, 60),
        reference_pair=(20, 60),
        cost_bps=5.0000002,
        min_fold_observations=20,
    )

    assert first.experiment_id != second.experiment_id


def test_parameter_cli_parses_grid_and_reference_pair() -> None:
    args = build_parser().parse_args(
        [
            "parameter-test",
            "--shorts",
            "8,16,24",
            "--longs",
            "48,72",
            "--reference-short",
            "16",
            "--reference-long",
            "72",
        ]
    )

    assert args.shorts == (8, 16, 24)
    assert args.longs == (48, 72)
    assert (args.reference_short, args.reference_long) == (16, 72)
