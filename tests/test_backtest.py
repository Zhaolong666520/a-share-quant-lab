from __future__ import annotations

import pandas as pd
import pytest

from finance_lab.backtest import BacktestResult, calculate_metrics, run_backtest
from finance_lab.sample import make_synthetic_daily_prices


def test_sma_signal_executes_on_next_day() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, short_window=5, long_window=20)
    frame = result.frame
    expected = frame["signal"].shift(1).fillna(0.0)
    assert frame["position"].reset_index(drop=True).equals(expected.reset_index(drop=True))


def test_momentum_signal_uses_only_prior_close_and_executes_next_day() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, strategy="momentum", momentum_lookback=20)
    frame = result.frame
    expected_signal = (frame["close"] / frame["close"].shift(20) - 1.0 > 0.0).astype(float)
    expected_position = expected_signal.shift(1).fillna(0.0)

    assert frame["signal"].reset_index(drop=True).equals(expected_signal.reset_index(drop=True))
    assert frame["position"].reset_index(drop=True).equals(
        expected_position.reset_index(drop=True)
    )
    assert (frame.loc[frame["position"] > 0, "signal_date"] < frame.loc[
        frame["position"] > 0, "trade_date"
    ]).all()


def test_cost_reduces_or_equals_gross_equity() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, short_window=5, long_window=20, cost_bps=10)
    gross_equity = (1 + result.frame["gross_return"]).cumprod().iloc[-1]
    assert result.frame["equity"].iloc[-1] <= gross_equity + 1e-12


def test_invalid_windows_are_rejected() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    with pytest.raises(ValueError, match="short_window"):
        run_backtest(prices, short_window=20, long_window=20)


@pytest.mark.parametrize("lookback", [0, -1, 2.5, True])
def test_invalid_momentum_lookback_is_rejected(lookback: object) -> None:
    prices = make_synthetic_daily_prices(periods=180)
    with pytest.raises(ValueError, match="momentum_lookback"):
        run_backtest(prices, strategy="momentum", momentum_lookback=lookback)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("strategy", "settings"),
    [
        ("momentum", {"momentum_lookback": 120}),
        ("sma", {"short_window": 20, "long_window": 120}),
    ],
)
def test_selected_indicator_requires_executable_warmup(
    strategy: str,
    settings: dict[str, int],
) -> None:
    prices = make_synthetic_daily_prices(periods=120)
    with pytest.raises(ValueError, match="可执行指标预热"):
        run_backtest(prices, strategy=strategy, **settings)


@pytest.mark.parametrize("cost_bps", [float("nan"), float("inf"), -1.0, 20_000.0])
def test_invalid_or_equity_breaking_cost_is_rejected(cost_bps: float) -> None:
    prices = make_synthetic_daily_prices(periods=180)
    with pytest.raises(ValueError, match="成本|净值"):
        run_backtest(prices, cost_bps=cost_bps, short_window=5, long_window=20)


def test_result_has_risk_metrics() -> None:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, short_window=5, long_window=20)
    assert result.metrics.observations > 100
    assert result.metrics.max_drawdown <= 0
    assert result.metrics.trade_sides >= 0


def test_legacy_backtest_result_constructor_keeps_default_strategy_settings() -> None:
    original = run_backtest(
        make_synthetic_daily_prices(periods=180),
        short_window=5,
        long_window=20,
    )
    reconstructed = BacktestResult(
        original.symbol,
        original.strategy,
        original.cost_bps,
        original.frame,
        original.metrics,
        original.benchmark_metrics,
    )

    assert reconstructed.short_window == 20
    assert reconstructed.long_window == 60
    assert reconstructed.momentum_lookback == 120


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
