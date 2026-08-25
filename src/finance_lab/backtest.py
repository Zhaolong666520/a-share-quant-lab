from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from finance_lab.validation import assert_valid_daily_prices


@dataclass(frozen=True)
class BacktestMetrics:
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float
    max_drawdown: float
    trade_sides: int
    exposure: float
    observations: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    strategy: str
    cost_bps: float
    frame: pd.DataFrame
    metrics: BacktestMetrics
    benchmark_metrics: BacktestMetrics
    short_window: int = 20
    long_window: int = 60
    momentum_lookback: int = 120


def calculate_metrics(
    returns: pd.Series,
    equity: pd.Series,
    position: pd.Series,
    turnover: pd.Series,
) -> BacktestMetrics:
    observations = int(returns.notna().sum())
    total_return = float(equity.iloc[-1] - 1.0) if observations else 0.0
    annualized_return = (
        float(equity.iloc[-1] ** (252 / observations) - 1.0) if observations else 0.0
    )
    volatility = float(returns.std(ddof=0) * np.sqrt(252)) if observations > 1 else 0.0
    sharpe = (
        float(returns.mean() / returns.std(ddof=0) * np.sqrt(252))
        if observations > 1 and returns.std(ddof=0) > 0
        else 0.0
    )
    equity_with_origin = pd.concat(
        [pd.Series([1.0], dtype=float), equity.reset_index(drop=True)],
        ignore_index=True,
    )
    drawdown = equity_with_origin / equity_with_origin.cummax() - 1.0
    return BacktestMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=volatility,
        sharpe_ratio=sharpe,
        max_drawdown=float(drawdown.min()),
        trade_sides=int(round(float(turnover.sum()))),
        exposure=float(position.mean()),
        observations=observations,
    )


def run_backtest(
    prices: pd.DataFrame,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    momentum_lookback: int = 120,
    cost_bps: float = 5.0,
) -> BacktestResult:
    """Backtest at next-day open; no signal may trade on the bar that created it."""
    assert_valid_daily_prices(prices)
    if strategy not in {"sma", "buy_hold", "momentum"}:
        raise ValueError("strategy 只能是 sma、buy_hold 或 momentum")
    if (
        isinstance(short_window, bool)
        or isinstance(long_window, bool)
        or not isinstance(short_window, int)
        or not isinstance(long_window, int)
        or short_window <= 0
        or long_window <= 0
        or short_window >= long_window
    ):
        raise ValueError("均线窗口必须满足 0 < short_window < long_window")
    if (
        isinstance(momentum_lookback, bool)
        or not isinstance(momentum_lookback, int)
        or momentum_lookback <= 0
    ):
        raise ValueError("momentum_lookback 必须是正整数")
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("交易成本必须是有限非负数")

    frame = prices.sort_values("trade_date").reset_index(drop=True).copy()
    frame["sma_short"] = frame["close"].rolling(short_window).mean()
    frame["sma_long"] = frame["close"].rolling(long_window).mean()
    frame["momentum_return"] = frame["close"] / frame["close"].shift(momentum_lookback) - 1.0
    if strategy == "sma":
        frame["signal"] = (frame["sma_short"] > frame["sma_long"]).astype(float)
    elif strategy == "momentum":
        frame["signal"] = (frame["momentum_return"] > 0.0).astype(float)
    else:
        frame["signal"] = 1.0

    # The close of day T creates the signal. shift(1) means execution at day T+1 open.
    frame["signal_date"] = frame["trade_date"].shift(1)
    frame["decision_signal"] = frame["signal"].shift(1)
    frame["position"] = frame["decision_signal"].fillna(0.0)
    frame["forward_open_return"] = frame["open"].shift(-1) / frame["open"] - 1.0
    frame["return_end_date"] = frame["trade_date"].shift(-1)
    indicator = (
        frame["sma_long"]
        if strategy == "sma"
        else frame["momentum_return"]
        if strategy == "momentum"
        else None
    )
    if indicator is not None and not (
        indicator.shift(1).notna() & frame["forward_open_return"].notna()
    ).any():
        raise ValueError("数据不足，所选策略没有完成可执行指标预热")
    frame["turnover"] = frame["position"].diff().abs().fillna(frame["position"].abs())
    frame["cost"] = frame["turnover"] * (cost_bps / 10_000.0)
    frame["gross_return"] = frame["position"] * frame["forward_open_return"]
    frame["net_return"] = frame["gross_return"] - frame["cost"]

    frame["benchmark_position"] = 1.0
    frame.loc[frame.index[0], "benchmark_position"] = 0.0
    frame["benchmark_turnover"] = (
        frame["benchmark_position"].diff().abs().fillna(frame["benchmark_position"].abs())
    )
    frame["benchmark_return"] = (
        frame["benchmark_position"] * frame["forward_open_return"]
        - frame["benchmark_turnover"] * (cost_bps / 10_000.0)
    )

    if (
        not np.isfinite(frame["net_return"].dropna()).all()
        or not np.isfinite(frame["benchmark_return"].dropna()).all()
        or (frame["net_return"].dropna() <= -1.0).any()
        or (frame["benchmark_return"].dropna() <= -1.0).any()
    ):
        raise ValueError("成本或收益导致非有限值或非正净值因子")

    evaluated = frame[frame["return_end_date"].notna()].copy()
    if evaluated.empty:
        raise ValueError("有效交易日不足，无法回测")
    evaluated["equity"] = (1.0 + evaluated["net_return"]).cumprod()
    evaluated["benchmark_equity"] = (1.0 + evaluated["benchmark_return"]).cumprod()
    evaluated["drawdown"] = evaluated["equity"] / evaluated["equity"].cummax() - 1.0

    metrics = calculate_metrics(
        evaluated["net_return"],
        evaluated["equity"],
        evaluated["position"],
        evaluated["turnover"],
    )
    benchmark_metrics = calculate_metrics(
        evaluated["benchmark_return"],
        evaluated["benchmark_equity"],
        evaluated["benchmark_position"],
        evaluated["benchmark_turnover"],
    )
    return BacktestResult(
        symbol=str(prices["symbol"].iloc[0]),
        strategy=strategy,
        cost_bps=cost_bps,
        short_window=short_window,
        long_window=long_window,
        momentum_lookback=momentum_lookback,
        frame=evaluated,
        metrics=metrics,
        benchmark_metrics=benchmark_metrics,
    )
