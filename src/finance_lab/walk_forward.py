from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import BacktestMetrics, BacktestResult, calculate_metrics, run_backtest
from finance_lab.experiment import data_fingerprint


@dataclass(frozen=True)
class ExecutionChecks:
    trade_sides: int
    missing_execution_prices: int
    invalid_signal_order: int
    signal_lag_mismatches: int
    signal_mismatches: int
    position_mismatches: int
    turnover_mismatches: int
    cost_mismatches: int

    @property
    def passed(self) -> bool:
        return all(
            value == 0
            for value in (
                self.missing_execution_prices,
                self.invalid_signal_order,
                self.signal_lag_mismatches,
                self.signal_mismatches,
                self.position_mismatches,
                self.turnover_mismatches,
                self.cost_mismatches,
            )
        )

    def to_dict(self) -> dict[str, int | bool]:
        return {**asdict(self), "passed": self.passed}


@dataclass(frozen=True)
class WalkForwardFold:
    fold_index: int
    train_start: date
    train_end: date
    oos_start: date
    oos_end: date
    metrics: BacktestMetrics
    benchmark_metrics: BacktestMetrics

    def to_dict(self) -> dict[str, object]:
        return {
            "fold_index": self.fold_index,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "oos_start": self.oos_start.isoformat(),
            "oos_end": self.oos_end.isoformat(),
            "metrics": self.metrics.to_dict(),
            "benchmark_metrics": self.benchmark_metrics.to_dict(),
        }


@dataclass(frozen=True)
class WalkForwardResult:
    experiment_id: str
    data_fingerprint: str
    symbol: str
    strategy: str
    first_oos_date: date
    fold_months: int
    min_development_observations: int
    min_fold_observations: int
    short_window: int
    long_window: int
    cost_bps: float
    full_result: BacktestResult
    folds: tuple[WalkForwardFold, ...]
    aggregate_frame: pd.DataFrame
    aggregate_metrics: BacktestMetrics
    aggregate_benchmark_metrics: BacktestMetrics
    execution_checks: ExecutionChecks

    @property
    def positive_folds(self) -> int:
        return sum(fold.metrics.total_return > 0 for fold in self.folds)

    @property
    def beats_benchmark_folds(self) -> int:
        return sum(
            fold.metrics.total_return > fold.benchmark_metrics.total_return
            for fold in self.folds
        )

    def settings_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "engine_version": __version__,
            "data_fingerprint_sha256": self.data_fingerprint,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "first_oos_date": self.first_oos_date.isoformat(),
            "fold_months": self.fold_months,
            "min_development_observations": self.min_development_observations,
            "min_fold_observations": self.min_fold_observations,
            "short_window": self.short_window,
            "long_window": self.long_window,
            "cost_bps": self.cost_bps,
        }


def _period_metrics(frame: pd.DataFrame, prefix: str = "") -> BacktestMetrics:
    return_column = "benchmark_return" if prefix else "net_return"
    returns = frame[return_column].astype(float)
    equity = (1.0 + returns).cumprod()
    position_column = "benchmark_position" if prefix else "position"
    turnover_column = "benchmark_turnover" if prefix else "turnover"
    return calculate_metrics(
        returns,
        equity,
        frame[position_column],
        frame[turnover_column],
    )


def _numeric_mismatch_count(left: pd.Series, right: pd.Series) -> int:
    return int((left.isna() | right.isna() | ((left - right).abs() > 1e-12)).sum())


def check_execution_consistency(
    full_frame: pd.DataFrame,
    oos_frame: pd.DataFrame,
    cost_bps: float,
) -> ExecutionChecks:
    expected_turnover_by_date = pd.Series(
        full_frame["position"]
        .diff()
        .abs()
        .fillna(full_frame["position"].abs())
        .to_numpy(),
        index=full_frame["trade_date"],
    )
    expected_turnover = oos_frame["trade_date"].map(expected_turnover_by_date)
    trades = oos_frame[expected_turnover > 1e-12].copy()
    missing_prices = int((trades["open"].isna() | (trades["open"] <= 0)).sum())
    invalid_signal_order = int(
        (
            oos_frame["signal_date"].isna()
            | (oos_frame["signal_date"] >= oos_frame["trade_date"])
        ).sum()
    )
    signal_by_date = full_frame.set_index("trade_date")["signal"]
    expected_signal = oos_frame["signal_date"].map(signal_by_date)
    signal_mismatches = _numeric_mismatch_count(oos_frame["decision_signal"], expected_signal)
    previous_date_by_date = pd.Series(
        full_frame["trade_date"].shift(1).to_numpy(),
        index=full_frame["trade_date"],
    )
    expected_signal_date = oos_frame["trade_date"].map(previous_date_by_date)
    signal_lag_mismatches = int(
        (
            expected_signal_date.isna()
            | (oos_frame["signal_date"] != expected_signal_date)
        ).sum()
    )
    position_mismatches = _numeric_mismatch_count(
        oos_frame["position"],
        expected_signal,
    )
    turnover_mismatches = _numeric_mismatch_count(oos_frame["turnover"], expected_turnover)
    expected_cost = expected_turnover * (cost_bps / 10_000.0)
    cost_mismatches = _numeric_mismatch_count(oos_frame["cost"], expected_cost)
    return ExecutionChecks(
        trade_sides=int(round(float(expected_turnover.sum()))),
        missing_execution_prices=missing_prices,
        invalid_signal_order=invalid_signal_order,
        signal_lag_mismatches=signal_lag_mismatches,
        signal_mismatches=signal_mismatches,
        position_mismatches=position_mismatches,
        turnover_mismatches=turnover_mismatches,
        cost_mismatches=cost_mismatches,
    )


def _walk_forward_id(
    first_oos_date: date,
    fold_months: int,
    strategy: str,
    short_window: int,
    long_window: int,
    cost_bps: float,
    min_development_observations: int,
    min_fold_observations: int,
    fingerprint: str,
) -> str:
    cost_token = f"{cost_bps:g}".replace(".", "p")
    version_token = __version__.replace(".", "")
    return (
        f"v{version_token}_{strategy}_{first_oos_date:%Y%m%d}_m{fold_months}_"
        f"d{min_development_observations}_f{min_fold_observations}_"
        f"s{short_window}_l{long_window}_c{cost_token}_{fingerprint[:8]}"
    )


def run_walk_forward(
    prices: pd.DataFrame,
    first_oos_date: date,
    fold_months: int = 12,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    cost_bps: float = 5.0,
    min_development_observations: int = 252,
    min_fold_observations: int = 60,
) -> WalkForwardResult:
    """Evaluate fixed parameters in consecutive, non-overlapping OOS windows."""
    if type(fold_months) is not int or fold_months <= 0:
        raise ValueError("fold_months 必须是大于0的整数")
    if type(min_development_observations) is not int or min_development_observations < 2:
        raise ValueError("min_development_observations 必须是至少为2的整数")
    if type(min_fold_observations) is not int or min_fold_observations < 2:
        raise ValueError("min_fold_observations 必须是至少为2的整数")

    full_result = run_backtest(
        prices,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
    )
    frame = full_result.frame.copy()
    first_oos = pd.Timestamp(first_oos_date)
    initial_development = frame[frame["return_end_date"] < first_oos]
    if len(initial_development) < min_development_observations:
        raise ValueError("首个样本外窗口之前的开发期数据不足")

    last_return_date = pd.Timestamp(frame["return_end_date"].max())
    fold_start = first_oos
    folds: list[WalkForwardFold] = []
    fold_frames: list[pd.DataFrame] = []
    while fold_start <= last_return_date:
        fold_end = fold_start + pd.DateOffset(months=fold_months)
        development = frame[frame["return_end_date"] < fold_start]
        out_of_sample = frame[
            (frame["return_end_date"] >= fold_start)
            & (frame["return_end_date"] < fold_end)
        ].copy()
        is_final_partial = fold_end > last_return_date
        if not is_final_partial and len(out_of_sample) < min_fold_observations:
            raise ValueError(
                f"完整窗口 {fold_start.date()} 至 {fold_end.date()} 数据不足："
                f"{len(out_of_sample)} < {min_fold_observations}"
            )
        if out_of_sample.empty:
            raise ValueError(f"窗口 {fold_start.date()} 至 {fold_end.date()} 没有收益观测")
        fold_index = len(folds) + 1
        out_of_sample["fold_index"] = fold_index
        folds.append(
            WalkForwardFold(
                fold_index=fold_index,
                train_start=pd.Timestamp(development["return_end_date"].iloc[0]).date(),
                train_end=pd.Timestamp(development["return_end_date"].iloc[-1]).date(),
                oos_start=pd.Timestamp(out_of_sample["return_end_date"].iloc[0]).date(),
                oos_end=pd.Timestamp(out_of_sample["return_end_date"].iloc[-1]).date(),
                metrics=_period_metrics(out_of_sample),
                benchmark_metrics=_period_metrics(out_of_sample, "benchmark_"),
            )
        )
        fold_frames.append(out_of_sample)
        fold_start = pd.Timestamp(fold_end)

    if not folds:
        raise ValueError("没有满足最小观测数的样本外窗口")

    aggregate_frame = pd.concat(fold_frames, ignore_index=True)
    aggregate_metrics = _period_metrics(aggregate_frame)
    aggregate_benchmark_metrics = _period_metrics(aggregate_frame, "benchmark_")
    fingerprint = data_fingerprint(prices)
    return WalkForwardResult(
        experiment_id=_walk_forward_id(
            first_oos_date,
            fold_months,
            strategy,
            short_window,
            long_window,
            cost_bps,
            min_development_observations,
            min_fold_observations,
            fingerprint,
        ),
        data_fingerprint=fingerprint,
        symbol=full_result.symbol,
        strategy=strategy,
        first_oos_date=first_oos_date,
        fold_months=fold_months,
        min_development_observations=min_development_observations,
        min_fold_observations=min_fold_observations,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
        full_result=full_result,
        folds=tuple(folds),
        aggregate_frame=aggregate_frame,
        aggregate_metrics=aggregate_metrics,
        aggregate_benchmark_metrics=aggregate_benchmark_metrics,
        execution_checks=check_execution_consistency(full_result.frame, aggregate_frame, cost_bps),
    )
