from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import BacktestMetrics, run_backtest
from finance_lab.experiment import data_fingerprint
from finance_lab.walk_forward import WalkForwardResult, run_walk_forward


@dataclass(frozen=True)
class CostScenario:
    cost_bps: float
    walk_forward: WalkForwardResult

    @property
    def metrics(self) -> BacktestMetrics:
        return self.walk_forward.aggregate_metrics

    @property
    def benchmark_metrics(self) -> BacktestMetrics:
        return self.walk_forward.aggregate_benchmark_metrics

    @property
    def fold_count(self) -> int:
        return len(self.walk_forward.folds)

    @property
    def positive_folds(self) -> int:
        return self.walk_forward.positive_folds

    @property
    def beats_benchmark_folds(self) -> int:
        return self.walk_forward.beats_benchmark_folds

    def to_dict(self) -> dict[str, object]:
        return {
            "cost_bps": self.cost_bps,
            "metrics": self.metrics.to_dict(),
            "benchmark_metrics": self.benchmark_metrics.to_dict(),
            "fold_count": self.fold_count,
            "positive_folds": self.positive_folds,
            "beats_benchmark_folds": self.beats_benchmark_folds,
            "execution_checks": self.walk_forward.execution_checks.to_dict(),
        }


@dataclass(frozen=True)
class CostSensitivityResult:
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
    cost_scenarios_bps: tuple[float, ...]
    scenarios: tuple[CostScenario, ...]

    @property
    def monotonic_non_increasing(self) -> bool:
        returns = [scenario.metrics.total_return for scenario in self.scenarios]
        return all(
            current <= previous + 1e-12
            for previous, current in zip(returns, returns[1:], strict=False)
        )

    @property
    def high_cost_return_change(self) -> float:
        return self.scenarios[-1].metrics.total_return - self.scenarios[0].metrics.total_return

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
            "cost_scenarios_bps": list(self.cost_scenarios_bps),
        }


def _cost_token(cost_bps: float) -> str:
    return f"{cost_bps:g}".replace(".", "p")


def _grid_fingerprint(cost_scenarios_bps: tuple[float, ...]) -> str:
    canonical = json.dumps(
        list(cost_scenarios_bps),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:10]


def _cost_experiment_id(
    first_oos_date: date,
    fold_months: int,
    short_window: int,
    long_window: int,
    min_development_observations: int,
    min_fold_observations: int,
    cost_scenarios_bps: tuple[float, ...],
    fingerprint: str,
) -> str:
    version_token = __version__.replace(".", "")
    grid_token = (
        f"{len(cost_scenarios_bps)}_"
        f"{_cost_token(cost_scenarios_bps[0])}-{_cost_token(cost_scenarios_bps[-1])}"
    )
    return (
        f"v{version_token}_sma_cost_{first_oos_date:%Y%m%d}_m{fold_months}_"
        f"d{min_development_observations}_f{min_fold_observations}_"
        f"s{short_window}_l{long_window}_g{grid_token}_"
        f"gc{_grid_fingerprint(cost_scenarios_bps)}_{fingerprint[:8]}"
    )


def _validate_cost_grid(cost_scenarios_bps: tuple[float, ...]) -> None:
    if len(cost_scenarios_bps) < 2:
        raise ValueError("cost_scenarios_bps 至少需要两个情景")
    if len(cost_scenarios_bps) > 20:
        raise ValueError("cost_scenarios_bps 最多允许20个情景")
    if any(not math.isfinite(value) for value in cost_scenarios_bps):
        raise ValueError("cost_scenarios_bps 只能包含有限数字")
    if any(value < 0 for value in cost_scenarios_bps):
        raise ValueError("cost_scenarios_bps 不能包含负数")
    if any(
        current <= previous
        for previous, current in zip(
            cost_scenarios_bps,
            cost_scenarios_bps[1:],
            strict=False,
        )
    ):
        raise ValueError("cost_scenarios_bps 必须严格递增且不能重复")


def _validate_equity_factors(
    prices: pd.DataFrame,
    cost_scenarios_bps: tuple[float, ...],
    strategy: str,
    short_window: int,
    long_window: int,
) -> None:
    zero_cost = run_backtest(
        prices,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        cost_bps=0.0,
    ).frame
    for cost_bps in cost_scenarios_bps:
        rate = cost_bps / 10_000.0
        strategy_net = zero_cost["gross_return"] - zero_cost["turnover"] * rate
        benchmark_net = (
            zero_cost["benchmark_position"] * zero_cost["forward_open_return"]
            - zero_cost["benchmark_turnover"] * rate
        )
        if (
            not np.isfinite(strategy_net).all()
            or not np.isfinite(benchmark_net).all()
            or (strategy_net <= -1.0).any()
            or (benchmark_net <= -1.0).any()
        ):
            raise ValueError(
                f"成本情景 {cost_bps:g} bps 会使净值因子小于等于0或非有限"
            )


def _assert_scenario_consistency(scenarios: tuple[CostScenario, ...]) -> None:
    reference = scenarios[0].walk_forward
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
    reference_frame = reference.aggregate_frame[consistency_columns]
    reference_boundaries = [
        (fold.train_start, fold.train_end, fold.oos_start, fold.oos_end)
        for fold in reference.folds
    ]
    for scenario in scenarios:
        candidate = scenario.walk_forward
        if not candidate.execution_checks.passed:
            raise RuntimeError(f"{scenario.cost_bps:g} bps 情景执行一致性检查失败")
        if not candidate.aggregate_frame[consistency_columns].equals(reference_frame):
            raise RuntimeError("成本情景之间的信号、仓位、换手、窗口或基准不一致")
        boundaries = [
            (fold.train_start, fold.train_end, fold.oos_start, fold.oos_end)
            for fold in candidate.folds
        ]
        if boundaries != reference_boundaries:
            raise RuntimeError("成本情景之间的折叠边界不一致")
        if candidate.aggregate_benchmark_metrics.to_dict() != (
            reference.aggregate_benchmark_metrics.to_dict()
        ):
            raise RuntimeError("成本情景之间的滚动基准指标不一致")

    for fold_index in range(len(reference.folds)):
        fold_returns = [
            scenario.walk_forward.folds[fold_index].metrics.total_return
            for scenario in scenarios
        ]
        if any(
            current > previous + 1e-12
            for previous, current in zip(fold_returns, fold_returns[1:], strict=False)
        ):
            raise RuntimeError(f"第{fold_index + 1}折收益未随成本单调不增加")


def run_cost_sensitivity(
    prices: pd.DataFrame,
    first_oos_date: date,
    cost_scenarios_bps: tuple[float, ...] = (5.0, 10.0, 20.0, 50.0),
    fold_months: int = 12,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    min_development_observations: int = 252,
    min_fold_observations: int = 60,
) -> CostSensitivityResult:
    """Run one fixed strategy through several hypothetical one-way cost levels."""
    if strategy != "sma":
        raise ValueError("成本压力实验目前只支持 sma 固定策略")
    _validate_cost_grid(cost_scenarios_bps)
    _validate_equity_factors(
        prices,
        cost_scenarios_bps,
        strategy,
        short_window,
        long_window,
    )

    scenarios = tuple(
        CostScenario(
            cost_bps=cost_bps,
            walk_forward=run_walk_forward(
                prices,
                first_oos_date=first_oos_date,
                fold_months=fold_months,
                strategy=strategy,
                short_window=short_window,
                long_window=long_window,
                cost_bps=cost_bps,
                min_development_observations=min_development_observations,
                min_fold_observations=min_fold_observations,
            ),
        )
        for cost_bps in cost_scenarios_bps
    )
    _assert_scenario_consistency(scenarios)
    fingerprint = data_fingerprint(prices)
    result = CostSensitivityResult(
        experiment_id=_cost_experiment_id(
            first_oos_date,
            fold_months,
            short_window,
            long_window,
            min_development_observations,
            min_fold_observations,
            cost_scenarios_bps,
            fingerprint,
        ),
        data_fingerprint=fingerprint,
        symbol=scenarios[0].walk_forward.symbol,
        strategy=strategy,
        first_oos_date=first_oos_date,
        fold_months=fold_months,
        min_development_observations=min_development_observations,
        min_fold_observations=min_fold_observations,
        short_window=short_window,
        long_window=long_window,
        cost_scenarios_bps=cost_scenarios_bps,
        scenarios=scenarios,
    )
    if not result.monotonic_non_increasing:
        raise RuntimeError("聚合收益未随成本单调不增加")
    return result
