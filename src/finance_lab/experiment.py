from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import (
    BacktestMetrics,
    BacktestResult,
    calculate_metrics,
    run_backtest,
)


@dataclass(frozen=True)
class PeriodSummary:
    label: str
    start_date: date
    end_date: date
    metrics: BacktestMetrics
    benchmark_metrics: BacktestMetrics

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "metrics": self.metrics.to_dict(),
            "benchmark_metrics": self.benchmark_metrics.to_dict(),
        }


@dataclass(frozen=True)
class SplitExperimentResult:
    experiment_id: str
    data_fingerprint: str
    symbol: str
    strategy: str
    split_date: date
    short_window: int
    long_window: int
    cost_bps: float
    full_result: BacktestResult
    development: PeriodSummary
    out_of_sample: PeriodSummary
    trades: pd.DataFrame

    def settings_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "engine_version": __version__,
            "data_fingerprint_sha256": self.data_fingerprint,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "split_date": self.split_date.isoformat(),
            "short_window": self.short_window,
            "long_window": self.long_window,
            "cost_bps": self.cost_bps,
        }


def _summarize_period(label: str, frame: pd.DataFrame) -> PeriodSummary:
    returns = frame["net_return"].astype(float)
    equity = (1.0 + returns).cumprod()
    benchmark_returns = frame["benchmark_return"].astype(float)
    benchmark_equity = (1.0 + benchmark_returns).cumprod()
    metrics = calculate_metrics(
        returns,
        equity,
        frame["position"],
        frame["turnover"],
    )
    benchmark_metrics = calculate_metrics(
        benchmark_returns,
        benchmark_equity,
        frame["benchmark_position"],
        frame["benchmark_turnover"],
    )
    return PeriodSummary(
        label=label,
        start_date=pd.Timestamp(frame["return_end_date"].iloc[0]).date(),
        end_date=pd.Timestamp(frame["return_end_date"].iloc[-1]).date(),
        metrics=metrics,
        benchmark_metrics=benchmark_metrics,
    )


def _extract_trades(frame: pd.DataFrame) -> pd.DataFrame:
    position_change = frame["position"].diff().fillna(frame["position"])
    trade_mask = position_change.abs() > 1e-12
    trades = frame.loc[
        trade_mask,
        [
            "trade_date",
            "signal_date",
            "open",
            "position",
            "turnover",
            "cost",
            "decision_signal",
        ],
    ].copy()
    trades["position_change"] = position_change.loc[trade_mask]
    trades["action"] = np.where(trades["position_change"] > 0, "BUY", "SELL")
    trades["position_before"] = trades["position"] - trades["position_change"]
    trades = trades.rename(
        columns={
            "open": "execution_open",
            "position": "position_after",
            "cost": "estimated_cost_fraction",
        }
    )
    columns = [
        "trade_date",
        "action",
        "execution_open",
        "position_before",
        "position_after",
        "position_change",
        "turnover",
        "estimated_cost_fraction",
        "signal_date",
        "decision_signal",
    ]
    return trades[columns].reset_index(drop=True)


def data_fingerprint(prices: pd.DataFrame) -> str:
    columns = [
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "volume_unit",
        "amount",
        "adjustment",
        "source",
        "ingested_at",
    ]
    canonical = prices[columns].sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    hashed = pd.util.hash_pandas_object(canonical, index=False).to_numpy().tobytes()
    return hashlib.sha256(hashed).hexdigest()


def _experiment_id(
    split_date: date,
    short_window: int,
    long_window: int,
    cost_bps: float,
    data_fingerprint: str,
) -> str:
    cost_token = f"{cost_bps:g}".replace(".", "p")
    version_token = __version__.replace(".", "")
    return (
        f"v{version_token}_{split_date:%Y%m%d}_s{short_window}_l{long_window}_"
        f"c{cost_token}_{data_fingerprint[:8]}"
    )


def run_split_experiment(
    prices: pd.DataFrame,
    split_date: date,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    cost_bps: float = 5.0,
    min_period_observations: int = 120,
) -> SplitExperimentResult:
    if strategy not in {"sma", "buy_hold"}:
        raise ValueError("固定切分实验目前只支持 sma 或 buy_hold")
    if min_period_observations < 2:
        raise ValueError("min_period_observations must be at least 2")
    full_result = run_backtest(
        prices,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
    )
    frame = full_result.frame.copy()
    split_timestamp = pd.Timestamp(split_date)
    development = frame[frame["return_end_date"] < split_timestamp].copy()
    out_of_sample = frame[frame["return_end_date"] >= split_timestamp].copy()
    if len(development) < min_period_observations:
        raise ValueError("开发期数据不足，请把切分日期向后调整")
    if len(out_of_sample) < min_period_observations:
        raise ValueError("样本外数据不足，请把切分日期向前调整")

    frame["experiment_period"] = np.where(
        frame["return_end_date"] < split_timestamp,
        "development",
        "out_of_sample",
    )
    full_result.frame["experiment_period"] = frame["experiment_period"]
    trades = _extract_trades(frame)
    trades["experiment_period"] = np.where(
        trades["trade_date"] < split_timestamp,
        "development",
        "out_of_sample",
    )
    fingerprint = data_fingerprint(prices)
    return SplitExperimentResult(
        experiment_id=_experiment_id(
            split_date,
            short_window,
            long_window,
            cost_bps,
            fingerprint,
        ),
        data_fingerprint=fingerprint,
        symbol=full_result.symbol,
        strategy=strategy,
        split_date=split_date,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
        full_result=full_result,
        development=_summarize_period("development", development),
        out_of_sample=_summarize_period("out_of_sample", out_of_sample),
        trades=trades,
    )
