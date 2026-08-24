from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import BacktestMetrics, run_backtest
from finance_lab.experiment import data_fingerprint
from finance_lab.validation import assert_valid_daily_prices
from finance_lab.walk_forward import WalkForwardResult, run_walk_forward


@dataclass(frozen=True)
class ParameterScenario:
    short_window: int
    long_window: int
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
            "short_window": self.short_window,
            "long_window": self.long_window,
            "metrics": self.metrics.to_dict(),
            "benchmark_metrics": self.benchmark_metrics.to_dict(),
            "fold_count": self.fold_count,
            "positive_folds": self.positive_folds,
            "beats_benchmark_folds": self.beats_benchmark_folds,
            "execution_checks": self.walk_forward.execution_checks.to_dict(),
        }


@dataclass(frozen=True)
class ParameterSensitivityResult:
    experiment_id: str
    data_fingerprint: str
    symbol: str
    strategy: str
    first_oos_date: date
    fold_months: int
    min_development_observations: int
    min_fold_observations: int
    short_windows: tuple[int, ...]
    long_windows: tuple[int, ...]
    reference_pair: tuple[int, int]
    cost_bps: float
    scenarios: tuple[ParameterScenario, ...]

    @property
    def total_scenarios(self) -> int:
        return len(self.scenarios)

    @property
    def reference_scenario(self) -> ParameterScenario:
        reference_short, reference_long = self.reference_pair
        return next(
            scenario
            for scenario in self.scenarios
            if (scenario.short_window, scenario.long_window)
            == (reference_short, reference_long)
        )

    @property
    def profitable_scenarios(self) -> int:
        return sum(scenario.metrics.total_return > 0 for scenario in self.scenarios)

    @property
    def beats_benchmark_scenarios(self) -> int:
        return sum(
            scenario.metrics.total_return > scenario.benchmark_metrics.total_return
            for scenario in self.scenarios
        )

    @property
    def return_range(self) -> float:
        returns = [scenario.metrics.total_return for scenario in self.scenarios]
        return max(returns) - min(returns)

    @property
    def median_total_return(self) -> float:
        returns = (scenario.metrics.total_return for scenario in self.scenarios)
        return float(statistics.median(returns))

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
            "short_windows": list(self.short_windows),
            "long_windows": list(self.long_windows),
            "reference_pair": list(self.reference_pair),
            "cost_bps": self.cost_bps,
        }


def _validate_window_axis(name: str, windows: tuple[int, ...]) -> None:
    if not 2 <= len(windows) <= 8:
        raise ValueError(f"{name}_windows 必须包含2到8个窗口")
    if any(type(value) is not int for value in windows):
        raise ValueError(f"{name}_windows 只能包含整数")
    if any(value <= 0 for value in windows):
        raise ValueError(f"{name}_windows 必须全部大于0")
    if any(
        current <= previous
        for previous, current in zip(windows, windows[1:], strict=False)
    ):
        raise ValueError(f"{name}_windows 必须严格递增且不能重复")


def _validate_parameter_grid(
    short_windows: tuple[int, ...],
    long_windows: tuple[int, ...],
    reference_pair: tuple[int, int],
) -> None:
    _validate_window_axis("short", short_windows)
    _validate_window_axis("long", long_windows)
    if max(short_windows) >= min(long_windows):
        raise ValueError("short_windows 的最大值必须小于 long_windows 的最小值")
    if (
        len(reference_pair) != 2
        or type(reference_pair[0]) is not int
        or type(reference_pair[1]) is not int
        or reference_pair[0] not in short_windows
        or reference_pair[1] not in long_windows
    ):
        raise ValueError("reference_pair 必须是参数网格中的一组整数窗口")


def _validate_cost(cost_bps: float) -> None:
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps 必须是大于等于0的有限数字")


def _validate_oos_warmup(
    prices: pd.DataFrame,
    first_oos_date: date,
    long_windows: tuple[int, ...],
) -> None:
    trade_dates = pd.to_datetime(prices["trade_date"], errors="coerce")
    available_observations = int((trade_dates < pd.Timestamp(first_oos_date)).sum())
    # The first OOS return uses a position decided one trade day earlier.
    # That decision day itself needs a fully formed long moving average.
    required_observations = max(long_windows) + 1
    if available_observations < required_observations:
        raise ValueError(
            "long_windows 在首个样本外窗口前没有完成预热："
            f"需要 {required_observations} 行，实际 {available_observations} 行"
        )


def _grid_fingerprint(
    short_windows: tuple[int, ...],
    long_windows: tuple[int, ...],
    reference_pair: tuple[int, int],
    cost_bps: float,
) -> str:
    canonical = json.dumps(
        {
            "cost_bps": cost_bps,
            "long_windows": list(long_windows),
            "reference_pair": list(reference_pair),
            "short_windows": list(short_windows),
        },
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:10]


def _cost_token(cost_bps: float) -> str:
    if float(cost_bps).is_integer():
        return str(int(cost_bps))
    return format(cost_bps, ".8g").replace(".", "p")


def _parameter_experiment_id(
    first_oos_date: date,
    fold_months: int,
    min_development_observations: int,
    min_fold_observations: int,
    short_windows: tuple[int, ...],
    long_windows: tuple[int, ...],
    reference_pair: tuple[int, int],
    cost_bps: float,
    fingerprint: str,
) -> str:
    version_token = __version__.replace(".", "")
    grid_hash = _grid_fingerprint(short_windows, long_windows, reference_pair, cost_bps)
    return (
        f"v{version_token}_sma_param_{first_oos_date:%Y%m%d}_m{fold_months}_"
        f"d{min_development_observations}_f{min_fold_observations}_c{_cost_token(cost_bps)}_"
        f"s{len(short_windows)}_l{len(long_windows)}_gp{grid_hash}_{fingerprint[:8]}"
    )


def _validate_equity_factors(
    prices: pd.DataFrame,
    short_windows: tuple[int, ...],
    long_windows: tuple[int, ...],
    cost_bps: float,
) -> None:
    rate = cost_bps / 10_000.0
    for short_window in short_windows:
        for long_window in long_windows:
            zero_cost = run_backtest(
                prices,
                strategy="sma",
                short_window=short_window,
                long_window=long_window,
                cost_bps=0.0,
            ).frame
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
                    f"参数 {short_window}/{long_window} 在 {cost_bps:g} bps 下会使"
                    "净值因子小于等于0或非有限"
                )


def _assert_scenario_consistency(scenarios: tuple[ParameterScenario, ...]) -> None:
    reference = scenarios[0].walk_forward
    consistency_columns = ["trade_date", "return_end_date", "fold_index", "benchmark_return"]
    reference_frame = reference.aggregate_frame[consistency_columns]
    reference_boundaries = [
        (fold.train_start, fold.train_end, fold.oos_start, fold.oos_end)
        for fold in reference.folds
    ]
    reference_benchmark = reference.aggregate_benchmark_metrics.to_dict()
    for scenario in scenarios:
        candidate = scenario.walk_forward
        label = f"{scenario.short_window}/{scenario.long_window}"
        if not candidate.execution_checks.passed:
            raise RuntimeError(f"参数 {label} 的执行一致性检查失败")
        if not candidate.aggregate_frame[consistency_columns].equals(reference_frame):
            raise RuntimeError(f"参数 {label} 的样本外日期、窗口或基准与其他情景不一致")
        boundaries = [
            (fold.train_start, fold.train_end, fold.oos_start, fold.oos_end)
            for fold in candidate.folds
        ]
        if boundaries != reference_boundaries:
            raise RuntimeError(f"参数 {label} 的滚动窗口边界与其他情景不一致")
        if candidate.aggregate_benchmark_metrics.to_dict() != reference_benchmark:
            raise RuntimeError(f"参数 {label} 的聚合基准指标与其他情景不一致")


def run_parameter_sensitivity(
    prices: pd.DataFrame,
    first_oos_date: date,
    short_windows: tuple[int, ...] = (10, 20, 30),
    long_windows: tuple[int, ...] = (40, 60, 90),
    reference_pair: tuple[int, int] = (20, 60),
    fold_months: int = 12,
    cost_bps: float = 5.0,
    min_development_observations: int = 252,
    min_fold_observations: int = 60,
) -> ParameterSensitivityResult:
    """Compare a fixed rectangular SMA grid on identical walk-forward windows."""
    assert_valid_daily_prices(prices)
    _validate_parameter_grid(short_windows, long_windows, reference_pair)
    _validate_cost(cost_bps)
    _validate_oos_warmup(prices, first_oos_date, long_windows)
    _validate_equity_factors(prices, short_windows, long_windows, cost_bps)

    scenarios = tuple(
        ParameterScenario(
            short_window=short_window,
            long_window=long_window,
            walk_forward=run_walk_forward(
                prices,
                first_oos_date=first_oos_date,
                fold_months=fold_months,
                strategy="sma",
                short_window=short_window,
                long_window=long_window,
                cost_bps=cost_bps,
                min_development_observations=min_development_observations,
                min_fold_observations=min_fold_observations,
            ),
        )
        for short_window in short_windows
        for long_window in long_windows
    )
    _assert_scenario_consistency(scenarios)
    fingerprint = data_fingerprint(prices)
    return ParameterSensitivityResult(
        experiment_id=_parameter_experiment_id(
            first_oos_date,
            fold_months,
            min_development_observations,
            min_fold_observations,
            short_windows,
            long_windows,
            reference_pair,
            cost_bps,
            fingerprint,
        ),
        data_fingerprint=fingerprint,
        symbol=scenarios[0].walk_forward.symbol,
        strategy="sma",
        first_oos_date=first_oos_date,
        fold_months=fold_months,
        min_development_observations=min_development_observations,
        min_fold_observations=min_fold_observations,
        short_windows=short_windows,
        long_windows=long_windows,
        reference_pair=reference_pair,
        cost_bps=cost_bps,
        scenarios=scenarios,
    )
