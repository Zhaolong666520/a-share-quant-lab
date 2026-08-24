from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.walk_forward import check_execution_consistency, run_walk_forward
from finance_lab.walk_forward_report import write_walk_forward_report


def test_walk_forward_builds_non_overlapping_chronological_folds() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        fold_months=6,
        min_fold_observations=20,
    )

    assert len(result.folds) >= 5
    assert sum(fold.metrics.observations for fold in result.folds) == (
        result.aggregate_metrics.observations
    )
    for previous, current in zip(result.folds, result.folds[1:], strict=False):
        assert previous.oos_end < current.oos_start
        assert previous.train_end < previous.oos_start


def test_future_prices_do_not_change_completed_folds() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    baseline = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        fold_months=6,
        min_fold_observations=20,
    )
    mutation_date = date(2024, 7, 1)
    changed = prices.copy()
    future_mask = changed["trade_date"].dt.date >= mutation_date
    changed.loc[future_mask, ["open", "high", "low", "close"]] *= 1.5
    mutated = run_walk_forward(
        changed,
        first_oos_date=date(2023, 1, 1),
        fold_months=6,
        min_fold_observations=20,
    )

    completed = [fold for fold in baseline.folds if fold.oos_end < mutation_date]
    assert completed
    for expected, actual in zip(
        completed,
        mutated.folds[: len(completed)],
        strict=True,
    ):
        assert expected.metrics.to_dict() == actual.metrics.to_dict()
        assert expected.benchmark_metrics.to_dict() == actual.benchmark_metrics.to_dict()
    changed_fold_index = next(
        index
        for index, fold in enumerate(baseline.folds)
        if fold.oos_start <= mutation_date <= fold.oos_end
    )
    assert baseline.folds[changed_fold_index].benchmark_metrics.total_return != pytest.approx(
        mutated.folds[changed_fold_index].benchmark_metrics.total_return
    )


def test_execution_checks_confirm_prior_day_signals() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        fold_months=12,
        min_fold_observations=20,
    )

    assert result.execution_checks.trade_sides > 0
    assert result.execution_checks.missing_execution_prices == 0
    assert result.execution_checks.invalid_signal_order == 0
    assert result.execution_checks.signal_lag_mismatches == 0
    assert result.execution_checks.signal_mismatches == 0
    assert result.execution_checks.position_mismatches == 0
    assert result.execution_checks.turnover_mismatches == 0
    assert result.execution_checks.cost_mismatches == 0
    assert result.execution_checks.passed


def test_execution_checks_detect_corrupted_decision_signal() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=20,
    )
    full_frame = result.full_result.frame.copy()
    aggregate = result.aggregate_frame.copy()
    trade_index = aggregate.index[aggregate["turnover"] > 0][0]
    aggregate.loc[trade_index, "decision_signal"] = 1.0 - float(
        aggregate.loc[trade_index, "decision_signal"]
    )

    checks = check_execution_consistency(full_frame, aggregate, result.cost_bps)

    assert checks.signal_mismatches == 1
    assert not checks.passed


def test_execution_checks_detect_erased_trade_and_non_trade_position() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=20,
    )
    aggregate = result.aggregate_frame.copy()
    trade_index = aggregate.index[aggregate["turnover"] > 0][0]
    aggregate.loc[trade_index, ["turnover", "cost"]] = 0.0
    non_trade_index = aggregate.index[aggregate["turnover"] == 0][0]
    aggregate.loc[non_trade_index, "decision_signal"] = 1.0 - float(
        aggregate.loc[non_trade_index, "decision_signal"]
    )
    aggregate.loc[non_trade_index, "position"] = 1.0 - float(
        aggregate.loc[non_trade_index, "position"]
    )

    checks = check_execution_consistency(
        result.full_result.frame,
        aggregate,
        result.cost_bps,
    )

    assert checks.turnover_mismatches == 1
    assert checks.cost_mismatches == 1
    assert checks.signal_mismatches == 1
    assert checks.position_mismatches == 1
    assert not checks.passed


def test_walk_forward_report_creates_html_json_chart_and_csv(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        fold_months=12,
        min_fold_observations=20,
    )
    outputs = write_walk_forward_report(result, get_paths(tmp_path))

    assert all(path.exists() for path in outputs)
    report_text = outputs[0].read_text(encoding="utf-8")
    assert "滚动检验" in report_text
    assert f"共 {len(result.folds)} 个窗口" in report_text
    assert result.folds[-1].oos_end.isoformat() in report_text
    csv_lines = outputs[3].read_text(encoding="utf-8-sig").splitlines()
    assert "fold_index" in csv_lines[0]
    assert len(csv_lines) == len(result.folds) + 1


def test_report_describes_buy_hold_without_calling_it_sma(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        strategy="buy_hold",
        min_fold_observations=20,
    )
    outputs = write_walk_forward_report(result, get_paths(tmp_path))
    report_text = outputs[0].read_text(encoding="utf-8")

    assert "固定使用买入持有策略" in report_text
    assert "固定使用20/60双均线" not in report_text


def test_short_final_partial_fold_is_included() -> None:
    prices = make_synthetic_daily_prices(periods=550)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        fold_months=12,
        min_fold_observations=60,
    )

    assert len(result.folds) == 2
    assert 2 <= result.folds[-1].metrics.observations < 60
    assert result.folds[-1].oos_end == prices["trade_date"].iloc[-1].date()


def test_threshold_changes_experiment_id_and_preserves_reports(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=550)
    first = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=20,
    )
    second = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_fold_observations=60,
    )
    first_outputs = write_walk_forward_report(first, get_paths(tmp_path))
    second_outputs = write_walk_forward_report(second, get_paths(tmp_path))

    assert first.experiment_id != second.experiment_id
    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)


def test_aggregate_is_exact_continuous_oos_slice() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_walk_forward(
        prices,
        first_oos_date=date(2023, 1, 1),
        fold_months=6,
        min_fold_observations=20,
    )
    aggregate_start = result.aggregate_frame["return_end_date"].min()
    aggregate_end = result.aggregate_frame["return_end_date"].max()
    expected = result.full_result.frame[
        (result.full_result.frame["return_end_date"] >= aggregate_start)
        & (
            result.full_result.frame["return_end_date"] <= aggregate_end
        )
    ]

    assert result.aggregate_frame["return_end_date"].tolist() == expected[
        "return_end_date"
    ].tolist()
    assert result.aggregate_frame["net_return"].tolist() == expected["net_return"].tolist()
    assert result.aggregate_metrics.trade_sides == round(float(expected["turnover"].sum()))


def test_walk_forward_rejects_invalid_fold_length() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match="fold_months"):
        run_walk_forward(prices, first_oos_date=date(2023, 1, 1), fold_months=0)


@pytest.mark.parametrize(
    ("kwargs", "expected_name"),
    [
        ({"fold_months": float("nan")}, "fold_months"),
        ({"fold_months": 1.5}, "fold_months"),
        ({"fold_months": True}, "fold_months"),
        ({"min_development_observations": float("nan")}, "min_development_observations"),
        ({"min_development_observations": float("inf")}, "min_development_observations"),
        ({"min_development_observations": True}, "min_development_observations"),
        ({"min_fold_observations": float("nan")}, "min_fold_observations"),
        ({"min_fold_observations": 20.5}, "min_fold_observations"),
        ({"min_fold_observations": False}, "min_fold_observations"),
    ],
)
def test_walk_forward_rejects_non_integer_window_settings(
    kwargs: dict[str, object],
    expected_name: str,
) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError, match=expected_name):
        run_walk_forward(  # type: ignore[arg-type]
            prices,
            first_oos_date=date(2023, 1, 1),
            **kwargs,
        )


def test_sparse_complete_window_raises_instead_of_hiding_later_data() -> None:
    prices = make_synthetic_daily_prices(periods=900)
    in_2023 = prices["trade_date"].dt.year == 2023
    sparse_2023 = prices.loc[in_2023].iloc[::10]
    prices = prices.loc[~in_2023]
    sparse_prices = (
        pd.concat([prices, sparse_2023], ignore_index=True)
        .sort_values("trade_date")
        .reset_index(drop=True)
    )

    with pytest.raises(ValueError, match="完整窗口"):
        run_walk_forward(
            sparse_prices,
            first_oos_date=date(2023, 1, 1),
            min_fold_observations=60,
        )
