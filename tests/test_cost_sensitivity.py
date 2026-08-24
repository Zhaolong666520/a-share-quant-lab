from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finance_lab.config import get_paths
from finance_lab.cost_sensitivity import run_cost_sensitivity
from finance_lab.cost_sensitivity_report import write_cost_sensitivity_report
from finance_lab.sample import make_synthetic_daily_prices


def test_higher_cost_never_improves_strategy_return() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        cost_scenarios_bps=(5.0, 10.0, 20.0, 50.0),
        min_fold_observations=20,
    )

    returns = [scenario.metrics.total_return for scenario in result.scenarios]
    assert returns == sorted(returns, reverse=True)
    assert result.monotonic_non_increasing
    assert result.high_cost_return_change < 0


def test_cost_scenarios_keep_signals_observations_and_trades_fixed() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=20,
    )

    assert {scenario.metrics.observations for scenario in result.scenarios} == {
        result.scenarios[0].metrics.observations
    }
    assert {scenario.metrics.trade_sides for scenario in result.scenarios} == {
        result.scenarios[0].metrics.trade_sides
    }
    assert {scenario.fold_count for scenario in result.scenarios} == {
        result.scenarios[0].fold_count
    }
    reference = result.scenarios[0].walk_forward
    consistency_columns = [
        "trade_date",
        "return_end_date",
        "signal",
        "decision_signal",
        "position",
        "turnover",
        "fold_index",
        "benchmark_return",
    ]
    for scenario in result.scenarios[1:]:
        assert scenario.walk_forward.aggregate_frame[consistency_columns].equals(
            reference.aggregate_frame[consistency_columns]
        )
        assert scenario.benchmark_metrics.to_dict() == (
            reference.aggregate_benchmark_metrics.to_dict()
        )
        assert scenario.walk_forward.execution_checks.passed


@pytest.mark.parametrize(
    "costs",
    [
        (5.0,),
        (10.0, 5.0),
        (5.0, 5.0),
        (-1.0, 5.0),
        (5.0, float("nan")),
        (5.0, float("inf")),
        tuple(float(value) for value in range(21)),
    ],
)
def test_invalid_cost_grid_is_rejected(costs: tuple[float, ...]) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="cost_scenarios_bps"):
        run_cost_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            cost_scenarios_bps=costs,
        )


def test_cost_report_creates_html_json_chart_and_csv(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=20,
    )
    outputs = write_cost_sensitivity_report(result, get_paths(tmp_path))

    assert all(path.exists() for path in outputs)
    report_text = outputs[0].read_text(encoding="utf-8")
    assert "交易成本压力测试" in report_text
    assert "不代表当前券商费率" in report_text
    assert f"{result.scenarios[-1].cost_bps:g} bps" in report_text
    assert "个百分点" in report_text
    csv_lines = outputs[3].read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_lines) == len(result.scenarios) + 1


def test_different_cost_grids_preserve_both_reports(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        cost_scenarios_bps=(5.0, 20.0),
        min_fold_observations=20,
    )
    second = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        cost_scenarios_bps=(5.0, 50.0),
        min_fold_observations=20,
    )
    first_outputs = write_cost_sensitivity_report(first, get_paths(tmp_path))
    second_outputs = write_cost_sensitivity_report(second, get_paths(tmp_path))

    assert first.experiment_id != second.experiment_id
    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)


def test_high_precision_cost_grids_have_distinct_ids() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        cost_scenarios_bps=(5.0, 5.0000001),
        min_fold_observations=20,
    )
    second = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        cost_scenarios_bps=(5.0, 5.0000002),
        min_fold_observations=20,
    )

    assert first.experiment_id != second.experiment_id
    assert len(first.experiment_id) < 180


def test_high_precision_costs_remain_distinct_in_report(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_cost_sensitivity(
        prices,
        first_oos_date=date(2023, 1, 1),
        cost_scenarios_bps=(5.0, 5.0000001),
        min_fold_observations=20,
    )
    outputs = write_cost_sensitivity_report(result, get_paths(tmp_path))
    report_text = outputs[0].read_text(encoding="utf-8")

    assert "5 bps" in report_text
    assert "5.0000001 bps" in report_text


def test_cost_that_breaks_positive_equity_factor_is_rejected() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="净值因子"):
        run_cost_sensitivity(
            prices,
            first_oos_date=date(2023, 1, 1),
            cost_scenarios_bps=(10_000.0, 20_000.0),
            min_fold_observations=20,
        )
