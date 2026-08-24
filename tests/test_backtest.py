from __future__ import annotations

import pandas as pd
import pytest

from finance_lab.backtest import calculate_metrics, run_backtest
from finance_lab.sample import make_synthetic_daily_prices


def test_sma_signal_executes_on_next_day() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, short_window=5, long_window=20)
    frame = result.frame
    expected = frame["signal"].shift(1).fillna(0.0)
    assert frame["position"].reset_index(drop=True).equals(expected.reset_index(drop=True))


def test_cost_reduces_or_equals_gross_equity() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, short_window=5, long_window=20, cost_bps=10)
    gross_equity = (1 + result.frame["gross_return"]).cumprod().iloc[-1]
    assert result.frame["equity"].iloc[-1] <= gross_equity + 1e-12


def test_invalid_windows_are_rejected() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    with pytest.raises(ValueError, match="short_window"):
        run_backtest(prices, short_window=20, long_window=20)


def test_result_has_risk_metrics() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, short_window=5, long_window=20)
    assert result.metrics.observations > 100
    assert result.metrics.max_drawdown <= 0
    assert result.metrics.trade_sides >= 0


def test_max_drawdown_includes_loss_from_starting_capital() -> None:
    strategy_returns = pd.Series([-0.10, 0.0])
    equity = (1.0 + strategy_returns).cumprod()
    metrics = calculate_metrics(
        returns=strategy_returns,
        equity=equity,
        position=pd.Series([1.0, 1.0]),
        turnover=pd.Series([1.0, 0.0]),
    )
    assert metrics.max_drawdown == pytest.approx(-0.10)
