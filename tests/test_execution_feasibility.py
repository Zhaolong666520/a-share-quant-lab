from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance_lab.backtest import run_backtest
from finance_lab.cli import build_parser
from finance_lab.config import get_paths
from finance_lab.execution_feasibility import (
    build_daily_bar_proxy,
    run_execution_feasibility,
    simulate_execution,
)
from finance_lab.execution_feasibility_report import write_execution_feasibility_report
from finance_lab.sample import make_synthetic_daily_prices


def _first_persistent_trade(frame: pd.DataFrame, action: str) -> int:
    position = frame["position"].reset_index(drop=True)
    for index in range(1, len(position) - 1):
        previous = float(position.iloc[index - 1])
        current = float(position.iloc[index])
        following = float(position.iloc[index + 1])
        if action == "BUY" and current > previous and following == current:
            return index
        if action == "SELL" and current < previous and following == current:
            return index
    raise AssertionError(f"No persistent {action} found in synthetic data")


def test_ideal_execution_state_machine_matches_existing_backtest() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    scenario = simulate_execution(
        baseline,
        first_oos_date=date(2022, 1, 5),
        name="ideal",
        description="ideal next-open execution",
    )

    assert scenario.full_frame["position"].tolist() == baseline.frame["position"].tolist()
    assert scenario.full_frame["turnover"].tolist() == baseline.frame["turnover"].tolist()
    assert scenario.full_frame["cost"].tolist() == baseline.frame["cost"].tolist()
    assert scenario.full_frame["net_return"].tolist() == baseline.frame["net_return"].tolist()
    assert scenario.execution_checks.passed


def test_blocked_buy_retains_cash_and_retries_once_allowed() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    trade_index = _first_persistent_trade(baseline.frame, "BUY")
    can_buy = pd.Series(True, index=range(len(baseline.frame)))
    can_buy.iloc[trade_index] = False
    buy_reason = pd.Series("", index=range(len(baseline.frame)), dtype=object)
    buy_reason.iloc[trade_index] = "TEST_BUY_BLOCK"
    boundary = pd.Timestamp(baseline.frame.loc[trade_index, "trade_date"]).date()

    scenario = simulate_execution(
        baseline,
        first_oos_date=boundary,
        name="blocked_buy",
        description="test buy block",
        can_buy_at_open=can_buy,
        buy_block_reason=buy_reason,
    )

    frame = scenario.full_frame.reset_index(drop=True)
    assert frame.loc[trade_index, "attempted_action"] == "BUY"
    assert not bool(frame.loc[trade_index, "filled"])
    assert frame.loc[trade_index, "position"] == frame.loc[trade_index, "position_before"]
    assert frame.loc[trade_index, "turnover"] == 0
    assert frame.loc[trade_index, "cost"] == 0
    assert frame.loc[trade_index + 1, "attempted_action"] == "BUY"
    assert bool(frame.loc[trade_index + 1, "filled"])
    assert frame.loc[trade_index + 1, "position"] == 1.0
    blocked_event = scenario.events.loc[
        scenario.events["trade_date"] == frame.loc[trade_index, "trade_date"]
    ].iloc[0]
    assert blocked_event["block_reason"] == "TEST_BUY_BLOCK"
    assert scenario.execution_checks.passed


def test_blocked_sell_retains_position_and_retries_once_allowed() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    trade_index = _first_persistent_trade(baseline.frame, "SELL")
    can_sell = pd.Series(True, index=range(len(baseline.frame)))
    can_sell.iloc[trade_index] = False
    sell_reason = pd.Series("", index=range(len(baseline.frame)), dtype=object)
    sell_reason.iloc[trade_index] = "TEST_SELL_BLOCK"
    boundary = pd.Timestamp(baseline.frame.loc[trade_index, "trade_date"]).date()

    scenario = simulate_execution(
        baseline,
        first_oos_date=boundary,
        name="blocked_sell",
        description="test sell block",
        can_sell_at_open=can_sell,
        sell_block_reason=sell_reason,
    )

    frame = scenario.full_frame.reset_index(drop=True)
    assert frame.loc[trade_index, "attempted_action"] == "SELL"
    assert not bool(frame.loc[trade_index, "filled"])
    assert frame.loc[trade_index, "position"] == 1.0
    assert frame.loc[trade_index, "turnover"] == 0
    assert frame.loc[trade_index + 1, "attempted_action"] == "SELL"
    assert bool(frame.loc[trade_index + 1, "filled"])
    assert frame.loc[trade_index + 1, "position"] == 0.0
    assert scenario.execution_checks.passed


def test_forced_delay_uses_an_older_signal_without_future_data() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    scenario = simulate_execution(
        baseline,
        first_oos_date=date(2022, 1, 5),
        name="forced_delay_1d",
        description="one extra trade-day delay",
        additional_delay_days=1,
    )

    expected_position = (
        baseline.frame["decision_signal"]
        .fillna(0.0)
        .shift(1, fill_value=0.0)
        .reset_index(drop=True)
    )
    evaluation_mask = scenario.full_frame["trade_date"] >= pd.Timestamp(date(2022, 1, 5))
    assert scenario.full_frame.loc[evaluation_mask, "position"].reset_index(drop=True).equals(
        expected_position.loc[evaluation_mask].reset_index(drop=True)
    )
    assert scenario.full_frame.loc[~evaluation_mask, "position"].reset_index(drop=True).equals(
        baseline.frame.loc[~evaluation_mask, "position"].reset_index(drop=True)
    )
    attempts = scenario.full_frame[scenario.full_frame["attempted_action"] != ""]
    assert (attempts["effective_signal_date"] < attempts["trade_date"]).all()
    assert scenario.execution_checks.passed


def test_daily_bar_proxy_flags_suspension_and_directional_locked_bars() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0, 110.0, 90.0],
            "high": [100.0, 110.0, 90.0],
            "low": [100.0, 110.0, 90.0],
            "close": [100.0, 110.0, 90.0],
            "volume": [0.0, 1_000.0, 1_000.0],
        }
    )
    flags = build_daily_bar_proxy(frame, lock_threshold_pct=0.095)

    assert not bool(flags.loc[0, "can_buy_at_open"])
    assert not bool(flags.loc[0, "can_sell_at_open"])
    assert flags.loc[0, "buy_block_reason"] == "ZERO_VOLUME_PROXY"
    assert not bool(flags.loc[1, "can_buy_at_open"])
    assert bool(flags.loc[1, "can_sell_at_open"])
    assert flags.loc[1, "buy_block_reason"] == "ONE_PRICE_UP_PROXY"
    assert bool(flags.loc[2, "can_buy_at_open"])
    assert not bool(flags.loc[2, "can_sell_at_open"])
    assert flags.loc[2, "sell_block_reason"] == "ONE_PRICE_DOWN_PROXY"


def test_daily_bar_proxy_is_an_explicit_ex_post_classification() -> None:
    one_price = pd.DataFrame(
        {
            "open": [100.0, 110.0],
            "high": [100.0, 110.0],
            "low": [100.0, 110.0],
            "close": [100.0, 110.0],
            "volume": [1_000.0, 1_000.0],
        }
    )
    traded_away = one_price.copy()
    traded_away.loc[1, "high"] = 112.0

    flagged = build_daily_bar_proxy(one_price)
    not_flagged = build_daily_bar_proxy(traded_away)

    assert not bool(flagged.loc[1, "can_buy_at_open"])
    assert bool(not_flagged.loc[1, "can_buy_at_open"])


@pytest.mark.parametrize(
    ("lock_threshold_pct", "forced_delay_days"),
    [
        (0.0, 1),
        (1.0, 1),
        (float("nan"), 1),
        (float("inf"), 1),
        (0.095, 0),
        (0.095, 6),
        (0.095, 1.5),
        (0.095, True),
    ],
)
def test_invalid_execution_stress_settings_are_rejected(
    lock_threshold_pct: float,
    forced_delay_days: int,
) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    with pytest.raises(ValueError):
        run_execution_feasibility(
            prices,
            first_oos_date=date(2023, 1, 1),
            lock_threshold_pct=lock_threshold_pct,
            forced_delay_days=forced_delay_days,
        )


def test_execution_feasibility_runs_three_consistent_scenarios() -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_execution_feasibility(
        prices,
        first_oos_date=date(2023, 1, 1),
        instrument_kind="index",
    )

    assert [scenario.name for scenario in result.scenarios] == [
        "ideal_next_open",
        "forced_delay_1d",
        "ex_post_daily_bar_proxy",
    ]
    assert result.ideal.metrics.observations == result.forced_delay.metrics.observations
    assert result.ideal.metrics.observations == result.proxy.metrics.observations
    assert all(
        scenario.metrics.trade_sides == scenario.filled_orders
        for scenario in result.scenarios
    )
    assert result.ideal.execution_checks.passed
    assert result.forced_delay.execution_checks.passed
    assert result.proxy.execution_checks.passed
    assert result.instrument_kind == "index"
    assert math.isfinite(result.return_change_vs_ideal(result.forced_delay))
    assert result.settings_dict()["proxy_evaluation_timing"] == (
        "ex_post_end_of_day_classification"
    )
    assert result.proxy.full_frame[["position", "turnover", "cost", "net_return"]].equals(
        result.ideal.full_frame[["position", "turnover", "cost", "net_return"]]
    )


def test_execution_report_creates_html_json_chart_and_events_csv(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    result = run_execution_feasibility(
        prices,
        first_oos_date=date(2023, 1, 1),
        instrument_kind="index",
    )
    outputs = write_execution_feasibility_report(result, get_paths(tmp_path))

    assert all(path.exists() and path.stat().st_size > 0 for path in outputs)
    report_text = outputs[0].read_text(encoding="utf-8")
    assert "执行可行性压力测试" in report_text
    assert "不是当前涨跌停规则" in report_text
    assert "指数本身不能直接交易" in report_text
    payload = json.loads(outputs[2].read_text(encoding="utf-8"))
    assert len(payload["scenarios"]) == 3
    assert all(item["execution_checks"]["passed"] for item in payload["scenarios"])
    csv_lines = outputs[3].read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_lines) > 1


def test_exact_proxy_settings_change_experiment_id_and_preserve_reports(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_execution_feasibility(
        prices,
        first_oos_date=date(2023, 1, 1),
        lock_threshold_pct=0.095,
    )
    second = run_execution_feasibility(
        prices,
        first_oos_date=date(2023, 1, 1),
        lock_threshold_pct=0.095000001,
    )
    first_outputs = write_execution_feasibility_report(first, get_paths(tmp_path))
    second_outputs = write_execution_feasibility_report(second, get_paths(tmp_path))

    assert first.experiment_id != second.experiment_id
    assert len(first.experiment_id) < 180
    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)


def test_volume_changes_execution_fingerprint_and_preserves_reports(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_execution_feasibility(
        prices,
        first_oos_date=date(2023, 1, 1),
        short_window=5,
        long_window=20,
    )
    event_date = first.ideal.events.iloc[0]["trade_date"]
    changed = prices.copy()
    changed.loc[changed["trade_date"] == event_date, "volume"] = 0.0
    second = run_execution_feasibility(
        changed,
        first_oos_date=date(2023, 1, 1),
        short_window=5,
        long_window=20,
    )
    first_outputs = write_execution_feasibility_report(first, get_paths(tmp_path))
    second_outputs = write_execution_feasibility_report(second, get_paths(tmp_path))

    assert first.data_fingerprint != second.data_fingerprint
    assert first.experiment_id != second.experiment_id
    assert second.proxy.blocked_attempts >= 1
    assert second.proxy_flagged_bars >= 1
    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)


def test_development_threshold_changes_experiment_id_and_preserves_reports(
    tmp_path: Path,
) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    first = run_execution_feasibility(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_development_observations=100,
    )
    second = run_execution_feasibility(
        prices,
        first_oos_date=date(2023, 1, 1),
        min_development_observations=252,
    )
    first_outputs = write_execution_feasibility_report(first, get_paths(tmp_path))
    second_outputs = write_execution_feasibility_report(second, get_paths(tmp_path))

    assert first.experiment_id != second.experiment_id
    assert first_outputs[0] != second_outputs[0]
    assert all(path.exists() for path in first_outputs + second_outputs)


def test_execution_cli_accepts_explicit_stress_settings() -> None:
    args = build_parser().parse_args(
        [
            "execution-test",
            "--symbol",
            "sh.510300",
            "--first-oos-date",
            "2022-01-04",
            "--short",
            "10",
            "--long",
            "40",
            "--cost-bps",
            "7.5",
            "--delay-days",
            "2",
            "--lock-threshold",
            "0.1",
        ]
    )

    assert args.symbol == "sh.510300"
    assert args.first_oos_date == date(2022, 1, 4)
    assert args.short == 10
    assert args.long == 40
    assert args.cost_bps == 7.5
    assert args.delay_days == 2
    assert args.lock_threshold == 0.1


@pytest.mark.parametrize("cost_bps", [10_000.0, 20_000.0])
def test_cost_that_can_make_equity_non_positive_is_rejected(cost_bps: float) -> None:
    prices = make_synthetic_daily_prices(periods=1000)

    with pytest.raises(ValueError, match="净值因子"):
        run_execution_feasibility(
            prices,
            first_oos_date=date(2023, 1, 1),
            cost_bps=cost_bps,
        )


def test_execution_events_are_counted_by_trade_date_at_oos_boundary() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    trade_index = _first_persistent_trade(baseline.frame, "BUY")
    boundary = pd.Timestamp(baseline.frame.loc[trade_index, "return_end_date"]).date()

    scenario = simulate_execution(
        baseline,
        first_oos_date=boundary,
        name="boundary",
        description="boundary event attribution",
    )

    assert (scenario.events["trade_date"] >= pd.Timestamp(boundary)).all()
    assert baseline.frame.loc[trade_index, "trade_date"] not in scenario.events[
        "trade_date"
    ].tolist()


def test_block_on_first_oos_trade_date_is_in_metrics_and_event_counts() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    trade_index = _first_persistent_trade(baseline.frame, "BUY")
    boundary = pd.Timestamp(baseline.frame.loc[trade_index, "trade_date"]).date()
    can_buy = pd.Series(True, index=range(len(baseline.frame)))
    can_buy.iloc[trade_index] = False

    scenario = simulate_execution(
        baseline,
        first_oos_date=boundary,
        name="boundary_block",
        description="block on first OOS trade date",
        can_buy_at_open=can_buy,
    )

    first_event = scenario.events.iloc[0]
    assert first_event["trade_date"] == pd.Timestamp(boundary)
    assert not bool(first_event["filled"])
    assert scenario.blocked_attempts >= 1
    assert scenario.oos_frame.iloc[0]["trade_date"] == pd.Timestamp(boundary)


def test_continuous_buy_blocks_charge_cost_only_when_finally_filled() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    trade_index = _first_persistent_trade(baseline.frame, "BUY")
    can_buy = pd.Series(True, index=range(len(baseline.frame)))
    can_buy.iloc[trade_index : trade_index + 2] = False
    boundary = pd.Timestamp(baseline.frame.loc[trade_index, "trade_date"]).date()

    scenario = simulate_execution(
        baseline,
        first_oos_date=boundary,
        name="continuous_block",
        description="two blocked attempts",
        can_buy_at_open=can_buy,
    )
    frame = scenario.full_frame.reset_index(drop=True)

    assert frame.loc[trade_index : trade_index + 1, "filled"].tolist() == [False, False]
    assert frame.loc[trade_index : trade_index + 1, "cost"].tolist() == [0.0, 0.0]
    assert bool(frame.loc[trade_index + 2, "filled"])
    assert frame.loc[trade_index + 2, "cost"] == pytest.approx(0.0005)


def test_pre_oos_block_cannot_change_oos_starting_state_or_metrics() -> None:
    prices = make_synthetic_daily_prices(periods=500)
    baseline = run_backtest(prices, short_window=5, long_window=20, cost_bps=5.0)
    trade_index = _first_persistent_trade(baseline.frame, "BUY")
    boundary = pd.Timestamp(baseline.frame.loc[trade_index + 2, "trade_date"]).date()
    can_buy = pd.Series(True, index=range(len(baseline.frame)))
    can_buy.iloc[trade_index] = False

    ideal = simulate_execution(
        baseline,
        first_oos_date=boundary,
        name="ideal_boundary",
        description="ideal boundary initialization",
    )
    pre_oos_block = simulate_execution(
        baseline,
        first_oos_date=boundary,
        name="pre_oos_block",
        description="pre-OOS block must be ignored",
        can_buy_at_open=can_buy,
    )

    columns = ["position", "turnover", "cost", "net_return"]
    assert pre_oos_block.oos_frame[columns].reset_index(drop=True).equals(
        ideal.oos_frame[columns].reset_index(drop=True)
    )
    assert pre_oos_block.blocked_attempts == 0
    assert pre_oos_block.metrics == ideal.metrics
