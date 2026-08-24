from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import BacktestMetrics, BacktestResult, calculate_metrics, run_backtest
from finance_lab.validation import assert_valid_daily_prices


@dataclass(frozen=True)
class ExecutionChecks:
    invalid_signal_order: int
    filled_while_blocked: int
    position_change_without_fill: int
    fill_target_mismatches: int
    turnover_mismatches: int
    cost_mismatches: int
    non_finite_returns: int
    non_positive_equity_factors: int

    @property
    def passed(self) -> bool:
        return all(value == 0 for value in asdict(self).values())

    def to_dict(self) -> dict[str, int | bool]:
        return {**asdict(self), "passed": self.passed}


@dataclass(frozen=True)
class ExecutionScenario:
    name: str
    description: str
    additional_delay_days: int
    full_frame: pd.DataFrame
    oos_frame: pd.DataFrame
    metrics: BacktestMetrics
    events: pd.DataFrame
    execution_checks: ExecutionChecks

    @property
    def trade_attempts(self) -> int:
        return int(len(self.events))

    @property
    def filled_orders(self) -> int:
        return int(self.events["filled"].sum()) if not self.events.empty else 0

    @property
    def blocked_attempts(self) -> int:
        return self.trade_attempts - self.filled_orders

    @property
    def unfilled_at_end(self) -> bool:
        final = self.full_frame.iloc[-1]
        return abs(float(final["target_position"]) - float(final["position"])) > 1e-12

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "additional_delay_days": self.additional_delay_days,
            "metrics": self.metrics.to_dict(),
            "trade_attempts": self.trade_attempts,
            "filled_orders": self.filled_orders,
            "blocked_attempts": self.blocked_attempts,
            "unfilled_at_end": self.unfilled_at_end,
            "execution_checks": self.execution_checks.to_dict(),
        }


@dataclass(frozen=True)
class ExecutionFeasibilityResult:
    experiment_id: str
    data_fingerprint: str
    symbol: str
    instrument_kind: str
    first_oos_date: date
    min_development_observations: int
    short_window: int
    long_window: int
    cost_bps: float
    forced_delay_days: int
    lock_threshold_pct: float
    proxy_flagged_bars: int
    benchmark_metrics: BacktestMetrics
    scenarios: tuple[ExecutionScenario, ...]

    @property
    def ideal(self) -> ExecutionScenario:
        return next(scenario for scenario in self.scenarios if scenario.name == "ideal_next_open")

    @property
    def forced_delay(self) -> ExecutionScenario:
        return next(
            scenario
            for scenario in self.scenarios
            if scenario.name.startswith("forced_delay_")
        )

    @property
    def proxy(self) -> ExecutionScenario:
        return next(
            scenario
            for scenario in self.scenarios
            if scenario.name == "ex_post_daily_bar_proxy"
        )

    def return_change_vs_ideal(self, scenario: ExecutionScenario) -> float:
        return scenario.metrics.total_return - self.ideal.metrics.total_return

    def position_difference_days(self, scenario: ExecutionScenario) -> int:
        left = self.ideal.oos_frame["position"].reset_index(drop=True)
        right = scenario.oos_frame["position"].reset_index(drop=True)
        return int(((left - right).abs() > 1e-12).sum())

    def settings_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "engine_version": __version__,
            "data_fingerprint_sha256": self.data_fingerprint,
            "symbol": self.symbol,
            "instrument_kind": self.instrument_kind,
            "first_oos_date": self.first_oos_date.isoformat(),
            "min_development_observations": self.min_development_observations,
            "short_window": self.short_window,
            "long_window": self.long_window,
            "cost_bps": self.cost_bps,
            "forced_delay_days": self.forced_delay_days,
            "lock_threshold_pct": self.lock_threshold_pct,
            "proxy_flagged_bars": self.proxy_flagged_bars,
            "proxy_evaluation_timing": "ex_post_end_of_day_classification",
        }


def _validate_lock_threshold(lock_threshold_pct: float) -> None:
    if not math.isfinite(lock_threshold_pct) or not 0 < lock_threshold_pct < 1:
        raise ValueError("lock_threshold_pct 必须是0到1之间的有限数字")


def build_daily_bar_proxy(
    frame: pd.DataFrame,
    lock_threshold_pct: float = 0.095,
) -> pd.DataFrame:
    """Classify completed bars ex post; this is not causal at-open information."""
    _validate_lock_threshold(lock_threshold_pct)
    required = {"open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"日线代理缺少字段: {missing}")

    numeric = frame[list(sorted(required))].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError("日线代理字段必须是有限数字")

    suspension_proxy = numeric["volume"] <= 0
    one_price_bar = np.isclose(
        numeric["high"].to_numpy(dtype=float),
        numeric["low"].to_numpy(dtype=float),
        rtol=1e-10,
        atol=1e-12,
    )
    previous_close = numeric["close"].shift(1)
    open_move = numeric["open"] / previous_close - 1.0
    one_price_series = pd.Series(one_price_bar, index=numeric.index)
    buy_lock_proxy = one_price_series & (open_move >= lock_threshold_pct)
    sell_lock_proxy = one_price_series & (open_move <= -lock_threshold_pct)

    buy_reason = np.select(
        [suspension_proxy, buy_lock_proxy],
        ["ZERO_VOLUME_PROXY", "ONE_PRICE_UP_PROXY"],
        default="",
    )
    sell_reason = np.select(
        [suspension_proxy, sell_lock_proxy],
        ["ZERO_VOLUME_PROXY", "ONE_PRICE_DOWN_PROXY"],
        default="",
    )
    return pd.DataFrame(
        {
            "can_buy_at_open": ~(suspension_proxy | buy_lock_proxy),
            "can_sell_at_open": ~(suspension_proxy | sell_lock_proxy),
            "buy_block_reason": buy_reason,
            "sell_block_reason": sell_reason,
            "suspension_proxy": suspension_proxy,
            "one_price_bar": one_price_series,
            "open_move_from_previous_close": open_move,
        }
    ).reset_index(drop=True)


def _boolean_series(
    values: pd.Series | None,
    length: int,
    name: str,
) -> pd.Series:
    if values is None:
        return pd.Series(True, index=range(length), dtype=bool)
    result = pd.Series(values).reset_index(drop=True)
    if len(result) != length:
        raise ValueError(f"{name} 长度必须与回测帧一致")
    if result.isna().any() or not result.map(
        lambda value: isinstance(value, (bool, np.bool_))
    ).all():
        raise ValueError(f"{name} 只能包含布尔值且不能有空值")
    return result.astype(bool)


def _reason_series(values: pd.Series | None, length: int, name: str) -> pd.Series:
    if values is None:
        return pd.Series("", index=range(length), dtype=object)
    result = pd.Series(values).reset_index(drop=True)
    if len(result) != length or result.isna().any():
        raise ValueError(f"{name} 长度必须一致且不能有空值")
    return result.astype(str)


def _numeric_mismatches(left: pd.Series, right: pd.Series) -> int:
    return int((left.isna() | right.isna() | ((left - right).abs() > 1e-12)).sum())


def _period_metrics(frame: pd.DataFrame, return_column: str = "net_return") -> BacktestMetrics:
    returns = frame[return_column].astype(float)
    equity = (1.0 + returns).cumprod()
    if return_column == "benchmark_return":
        position = frame["benchmark_position"]
        turnover = frame["benchmark_turnover"]
    else:
        position = frame["position"]
        turnover = frame["turnover"]
    return calculate_metrics(returns, equity, position, turnover)


def simulate_execution(
    baseline: BacktestResult,
    first_oos_date: date,
    name: str,
    description: str,
    additional_delay_days: int = 0,
    can_buy_at_open: pd.Series | None = None,
    can_sell_at_open: pd.Series | None = None,
    buy_block_reason: pd.Series | None = None,
    sell_block_reason: pd.Series | None = None,
) -> ExecutionScenario:
    """Apply delayed targets and directional fill permissions to a baseline frame."""
    if type(additional_delay_days) is not int or not 0 <= additional_delay_days <= 5:
        raise ValueError("additional_delay_days 必须是0到5之间的整数")
    frame = baseline.frame.reset_index(drop=True).copy()
    length = len(frame)
    can_buy = _boolean_series(can_buy_at_open, length, "can_buy_at_open")
    can_sell = _boolean_series(can_sell_at_open, length, "can_sell_at_open")
    buy_reasons = _reason_series(buy_block_reason, length, "buy_block_reason")
    sell_reasons = _reason_series(sell_block_reason, length, "sell_block_reason")

    decision_signal = frame["decision_signal"].fillna(0.0).astype(float)
    evaluation_mask = frame["trade_date"] >= pd.Timestamp(first_oos_date)
    delayed_target = decision_signal.shift(
        additional_delay_days,
        fill_value=0.0,
    )
    effective_signal_date = frame["signal_date"].shift(additional_delay_days)
    frame["target_position"] = delayed_target.where(
        evaluation_mask,
        frame["position"].astype(float),
    )
    frame["effective_signal_date"] = effective_signal_date.where(
        evaluation_mask,
        frame["signal_date"],
    )

    position_before: list[float] = []
    positions: list[float] = []
    actions: list[str] = []
    filled_values: list[bool] = []
    allowed_values: list[bool] = []
    reasons: list[str] = []
    current_position = 0.0
    for index, target_value in enumerate(frame["target_position"]):
        target = float(target_value)
        before = current_position
        action = ""
        filled = False
        allowed = True
        reason = ""
        if target > current_position + 1e-12:
            action = "BUY"
            allowed = not bool(evaluation_mask.iloc[index]) or bool(can_buy.iloc[index])
            if allowed:
                current_position = target
                filled = True
            else:
                reason = str(buy_reasons.iloc[index]) or "BUY_NOT_ALLOWED"
        elif target < current_position - 1e-12:
            action = "SELL"
            allowed = not bool(evaluation_mask.iloc[index]) or bool(can_sell.iloc[index])
            if allowed:
                current_position = target
                filled = True
            else:
                reason = str(sell_reasons.iloc[index]) or "SELL_NOT_ALLOWED"
        position_before.append(before)
        positions.append(current_position)
        actions.append(action)
        filled_values.append(filled)
        allowed_values.append(allowed)
        reasons.append(reason)

    frame["position_before"] = position_before
    frame["position"] = positions
    frame["attempted_action"] = actions
    frame["filled"] = filled_values
    frame["allowed_for_action"] = allowed_values
    frame["block_reason"] = reasons
    frame["turnover"] = (frame["position"] - frame["position_before"]).abs()
    frame["cost"] = frame["turnover"] * (baseline.cost_bps / 10_000.0)
    frame["gross_return"] = frame["position"] * frame["forward_open_return"]
    frame["net_return"] = frame["gross_return"] - frame["cost"]
    frame["equity"] = (1.0 + frame["net_return"]).cumprod()
    frame["drawdown"] = frame["equity"] / frame["equity"].cummax() - 1.0

    attempted = frame["attempted_action"] != ""
    changed = (frame["position"] - frame["position_before"]).abs() > 1e-12
    expected_turnover = (frame["position"] - frame["position_before"]).abs()
    expected_cost = expected_turnover * (baseline.cost_bps / 10_000.0)
    factors = 1.0 + frame["net_return"]
    invalid_signal_order = int(
        (
            attempted
            & (
                frame["effective_signal_date"].isna()
                | (frame["effective_signal_date"] >= frame["trade_date"])
            )
        ).sum()
    )
    checks = ExecutionChecks(
        invalid_signal_order=invalid_signal_order,
        filled_while_blocked=int((frame["filled"] & ~frame["allowed_for_action"]).sum()),
        position_change_without_fill=int((changed & ~frame["filled"]).sum()),
        fill_target_mismatches=int(
            (frame["filled"] & ((frame["position"] - frame["target_position"]).abs() > 1e-12)).sum()
        ),
        turnover_mismatches=_numeric_mismatches(frame["turnover"], expected_turnover),
        cost_mismatches=_numeric_mismatches(frame["cost"], expected_cost),
        non_finite_returns=int((~np.isfinite(frame["net_return"])).sum()),
        non_positive_equity_factors=int((~np.isfinite(factors) | (factors <= 0)).sum()),
    )

    oos = frame[frame["trade_date"] >= pd.Timestamp(first_oos_date)].copy()
    if len(oos) < 2:
        raise ValueError("first_oos_date 之后至少需要2个收益观测")
    event_columns = [
        "trade_date",
        "return_end_date",
        "effective_signal_date",
        "attempted_action",
        "filled",
        "block_reason",
        "target_position",
        "position_before",
        "position",
        "turnover",
        "cost",
    ]
    events = oos.loc[oos["attempted_action"] != "", event_columns].copy()
    events.insert(0, "scenario", name)
    return ExecutionScenario(
        name=name,
        description=description,
        additional_delay_days=additional_delay_days,
        full_frame=frame,
        oos_frame=oos,
        metrics=_period_metrics(oos),
        events=events.reset_index(drop=True),
        execution_checks=checks,
    )


def _config_fingerprint(
    short_window: int,
    long_window: int,
    cost_bps: float,
    forced_delay_days: int,
    lock_threshold_pct: float,
    instrument_kind: str,
    min_development_observations: int,
) -> str:
    canonical = json.dumps(
        {
            "cost_bps": cost_bps,
            "forced_delay_days": forced_delay_days,
            "instrument_kind": instrument_kind,
            "lock_threshold_pct": lock_threshold_pct,
            "long_window": long_window,
            "min_development_observations": min_development_observations,
            "short_window": short_window,
        },
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:10]


def _execution_data_fingerprint(prices: pd.DataFrame) -> str:
    columns = [
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source",
    ]
    canonical = prices[columns].sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    hashed = pd.util.hash_pandas_object(canonical, index=False).to_numpy().tobytes()
    return hashlib.sha256(hashed).hexdigest()


def _cost_token(cost_bps: float) -> str:
    if float(cost_bps).is_integer():
        return str(int(cost_bps))
    return format(cost_bps, ".8g").replace(".", "p")


def _experiment_id(
    first_oos_date: date,
    short_window: int,
    long_window: int,
    cost_bps: float,
    forced_delay_days: int,
    lock_threshold_pct: float,
    instrument_kind: str,
    min_development_observations: int,
    fingerprint: str,
) -> str:
    version_token = __version__.replace(".", "")
    threshold_bps = int(round(lock_threshold_pct * 10_000))
    config_hash = _config_fingerprint(
        short_window,
        long_window,
        cost_bps,
        forced_delay_days,
        lock_threshold_pct,
        instrument_kind,
        min_development_observations,
    )
    return (
        f"v{version_token}_sma_exec_{first_oos_date:%Y%m%d}_s{short_window}_l{long_window}_"
        f"c{_cost_token(cost_bps)}_d{forced_delay_days}_t{threshold_bps}_"
        f"n{min_development_observations}_"
        f"gx{config_hash}_{fingerprint[:8]}"
    )


def run_execution_feasibility(
    prices: pd.DataFrame,
    first_oos_date: date,
    instrument_kind: str = "unknown",
    short_window: int = 20,
    long_window: int = 60,
    cost_bps: float = 5.0,
    forced_delay_days: int = 1,
    lock_threshold_pct: float = 0.095,
    min_development_observations: int = 252,
) -> ExecutionFeasibilityResult:
    """Compare ideal, forced-delay, and conservative daily-bar proxy execution."""
    assert_valid_daily_prices(prices)
    if type(short_window) is not int or type(long_window) is not int:
        raise ValueError("均线窗口必须是整数")
    if short_window <= 0 or short_window >= long_window:
        raise ValueError("均线窗口必须满足 0 < short_window < long_window")
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps 必须是大于等于0的有限数字")
    if type(forced_delay_days) is not int or not 1 <= forced_delay_days <= 5:
        raise ValueError("forced_delay_days 必须是1到5之间的整数")
    _validate_lock_threshold(lock_threshold_pct)
    if type(min_development_observations) is not int or min_development_observations < 2:
        raise ValueError("min_development_observations 必须是至少为2的整数")
    allowed_kinds = {"index", "etf", "stock", "unknown"}
    if instrument_kind not in allowed_kinds:
        raise ValueError(f"instrument_kind 必须属于 {sorted(allowed_kinds)}")

    sorted_open = prices.sort_values("trade_date")["open"].astype(float)
    forward_open_return = sorted_open.shift(-1) / sorted_open - 1.0
    worst_open_return = min(0.0, float(forward_open_return.dropna().min()))
    minimum_possible_factor = 1.0 + worst_open_return - cost_bps / 10_000.0
    if minimum_possible_factor <= 0:
        raise ValueError(
            "cost_bps 对当前价格序列过高，可能使单日净值因子小于等于0"
        )

    baseline = run_backtest(
        prices,
        strategy="sma",
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
    )
    development = baseline.frame[
        baseline.frame["return_end_date"] < pd.Timestamp(first_oos_date)
    ]
    required_development = max(min_development_observations, long_window + 1)
    if len(development) < required_development:
        raise ValueError(
            "首个样本外日期之前的数据不足："
            f"需要 {required_development} 个收益观测，实际 {len(development)} 个"
        )

    flags = build_daily_bar_proxy(baseline.frame, lock_threshold_pct)
    ideal = simulate_execution(
        baseline,
        first_oos_date,
        name="ideal_next_open",
        description="信号后下一个交易日开盘理想成交",
    )
    forced_delay = simulate_execution(
        baseline,
        first_oos_date,
        name=f"forced_delay_{forced_delay_days}d",
        description=f"所有目标仓位统一额外延迟{forced_delay_days}个交易日",
        additional_delay_days=forced_delay_days,
    )
    proxy = simulate_execution(
        baseline,
        first_oos_date,
        name="ex_post_daily_bar_proxy",
        description="收盘后识别零成交量与全天单一价格跳空的事后日线阻塞代理",
        can_buy_at_open=flags["can_buy_at_open"],
        can_sell_at_open=flags["can_sell_at_open"],
        buy_block_reason=flags["buy_block_reason"],
        sell_block_reason=flags["sell_block_reason"],
    )
    if not ideal.full_frame[["position", "turnover", "cost", "net_return"]].equals(
        baseline.frame.reset_index(drop=True)[["position", "turnover", "cost", "net_return"]]
    ):
        raise RuntimeError("理想执行状态机未能复现现有回测")
    scenarios = (ideal, forced_delay, proxy)
    for scenario in scenarios:
        if not scenario.execution_checks.passed:
            raise RuntimeError(f"{scenario.name} 执行一致性检查失败")

    oos_mask = baseline.frame["trade_date"] >= pd.Timestamp(first_oos_date)
    proxy_flagged_bars = int(
        (
            ~flags.loc[oos_mask.to_numpy(), "can_buy_at_open"]
            | ~flags.loc[oos_mask.to_numpy(), "can_sell_at_open"]
        ).sum()
    )
    benchmark_metrics = _period_metrics(
        baseline.frame.loc[oos_mask].copy(),
        return_column="benchmark_return",
    )
    fingerprint = _execution_data_fingerprint(prices)
    return ExecutionFeasibilityResult(
        experiment_id=_experiment_id(
            first_oos_date,
            short_window,
            long_window,
            cost_bps,
            forced_delay_days,
            lock_threshold_pct,
            instrument_kind,
            min_development_observations,
            fingerprint,
        ),
        data_fingerprint=fingerprint,
        symbol=baseline.symbol,
        instrument_kind=instrument_kind,
        first_oos_date=first_oos_date,
        min_development_observations=min_development_observations,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
        forced_delay_days=forced_delay_days,
        lock_threshold_pct=lock_threshold_pct,
        proxy_flagged_bars=proxy_flagged_bars,
        benchmark_metrics=benchmark_metrics,
        scenarios=scenarios,
    )
