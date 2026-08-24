from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finance_lab.config import get_paths
from finance_lab.experiment import run_split_experiment
from finance_lab.experiment_report import write_split_experiment_report
from finance_lab.sample import make_synthetic_daily_prices


def test_split_experiment_partitions_all_observations() -> None:
    prices = make_synthetic_daily_prices(periods=900)
    result = run_split_experiment(prices, split_date=date(2024, 1, 1))
    period_total = (
        result.development.metrics.observations + result.out_of_sample.metrics.observations
    )
    assert period_total == result.full_result.metrics.observations
    assert result.development.end_date < date(2024, 1, 1)
    assert result.out_of_sample.start_date >= date(2024, 1, 1)


def test_trade_log_contains_only_position_changes() -> None:
    prices = make_synthetic_daily_prices(periods=900)
    result = run_split_experiment(prices, split_date=date(2024, 1, 1))
    assert not result.trades.empty
    assert set(result.trades["action"]).issubset({"BUY", "SELL"})
    assert (result.trades["position_change"].abs() > 0).all()
    assert set(result.trades["experiment_period"]) == {"development", "out_of_sample"}
    signal_by_date = result.full_result.frame.set_index("trade_date")["signal"]
    for trade in result.trades.itertuples():
        assert trade.decision_signal == pytest.approx(signal_by_date.loc[trade.signal_date])


def test_split_rejects_too_short_period() -> None:
    prices = make_synthetic_daily_prices(periods=900)
    with pytest.raises(ValueError, match="开发期数据不足"):
        run_split_experiment(prices, split_date=date(2022, 3, 1))


def test_experiment_report_creates_html_json_chart_and_trades(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=900)
    result = run_split_experiment(prices, split_date=date(2024, 1, 1))
    outputs = write_split_experiment_report(result, get_paths(tmp_path))
    assert all(path.exists() for path in outputs)
    assert "样本外" in outputs[0].read_text(encoding="utf-8")
    trades_csv = outputs[3].read_text(encoding="utf-8-sig")
    assert "experiment_period" in trades_csv
    assert "signal_date" in trades_csv
    assert "decision_signal" in trades_csv


def test_development_metrics_do_not_consume_out_of_sample_prices() -> None:
    prices = make_synthetic_daily_prices(periods=900)
    split_date = date(2024, 1, 1)
    baseline = run_split_experiment(prices, split_date=split_date)

    changed = prices.copy()
    future_mask = changed["trade_date"].dt.date >= split_date
    changed.loc[future_mask, ["open", "high", "low", "close"]] *= 1.5
    mutated = run_split_experiment(changed, split_date=split_date)

    assert baseline.development.metrics.to_dict() == mutated.development.metrics.to_dict()
    assert baseline.development.end_date == mutated.development.end_date
    assert baseline.out_of_sample.benchmark_metrics.total_return != pytest.approx(
        mutated.out_of_sample.benchmark_metrics.total_return
    )


def test_different_experiment_configs_preserve_both_reports(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=900)
    first = run_split_experiment(
        prices,
        split_date=date(2024, 1, 1),
        short_window=20,
        long_window=60,
    )
    second = run_split_experiment(
        prices,
        split_date=date(2024, 2, 1),
        short_window=10,
        long_window=40,
    )
    first_outputs = write_split_experiment_report(first, get_paths(tmp_path))
    second_outputs = write_split_experiment_report(second, get_paths(tmp_path))

    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)
