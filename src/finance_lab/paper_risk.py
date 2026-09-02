"""Deterministic risk snapshots for forward paper-trading valuations."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

RISK_DAILY_COLUMNS = {"trade_date", "equity", "shares", "close_price", "drawdown"}
RISK_RECONCILIATION_TOLERANCE = 1e-10


@dataclass(frozen=True)
class PaperRiskPolicy:
    """Fixed educational limits for extended paper observation."""

    minimum_return_observations: int = 60
    maximum_drawdown: float = 0.15
    maximum_annualized_volatility: float = 0.30
    maximum_consecutive_losing_days: int = 5


@dataclass(frozen=True)
class PaperRiskSnapshot:
    valuation_observations: int
    return_observations: int
    annualized_volatility: float | None
    max_drawdown: float
    current_drawdown: float
    worst_daily_return: float | None
    losing_days: int
    max_consecutive_losing_days: int
    average_exposure: float
    current_exposure: float
    breach_codes: tuple[str, ...]
    gate_status: str
    gate_passed: bool


DEFAULT_PAPER_RISK_POLICY = PaperRiskPolicy()


def calculate_paper_risk(
    daily: pd.DataFrame,
    policy: PaperRiskPolicy = DEFAULT_PAPER_RISK_POLICY,
) -> PaperRiskSnapshot:
    """Calculate one account's end-of-day risk snapshot from ledger valuations."""
    _validate_policy(policy)
    frame = _validated_daily(daily)
    equity = frame["equity"]
    returns = equity.pct_change(fill_method=None).dropna()
    drawdown = equity / equity.cummax() - 1.0
    exposure = frame["shares"].astype(float) * frame["close_price"].astype(float) / equity
    annualized_volatility = (
        float(returns.std(ddof=0) * math.sqrt(252)) if len(returns) >= 2 else None
    )
    loss_flags = [bool(value < 0.0) for value in returns]
    longest_streak = _longest_true_streak(loss_flags)
    max_drawdown = float(drawdown.min())
    breaches: list[str] = []
    if max_drawdown < -policy.maximum_drawdown:
        breaches.append("MAX_DRAWDOWN")
    if (
        annualized_volatility is not None
        and annualized_volatility > policy.maximum_annualized_volatility
    ):
        breaches.append("ANNUALIZED_VOLATILITY")
    if longest_streak > policy.maximum_consecutive_losing_days:
        breaches.append("LOSING_STREAK")
    breach_codes = tuple(breaches)
    if breach_codes:
        gate_status = "risk_limit_breached"
    elif len(returns) < policy.minimum_return_observations:
        gate_status = "insufficient_history"
    else:
        gate_status = "extended_paper_observation"
    return PaperRiskSnapshot(
        valuation_observations=len(frame),
        return_observations=len(returns),
        annualized_volatility=annualized_volatility,
        max_drawdown=max_drawdown,
        current_drawdown=float(drawdown.iloc[-1]),
        worst_daily_return=float(returns.min()) if not returns.empty else None,
        losing_days=sum(loss_flags),
        max_consecutive_losing_days=longest_streak,
        average_exposure=float(exposure.mean()),
        current_exposure=float(exposure.iloc[-1]),
        breach_codes=breach_codes,
        gate_status=gate_status,
        gate_passed=gate_status == "extended_paper_observation",
    )


def _validate_policy(policy: PaperRiskPolicy) -> None:
    if (
        type(policy.minimum_return_observations) is not int
        or policy.minimum_return_observations < 2
    ):
        raise ValueError("最少收益观察数必须是至少为 2 的整数")
    if (
        isinstance(policy.maximum_drawdown, bool)
        or not isinstance(policy.maximum_drawdown, (int, float))
        or not math.isfinite(policy.maximum_drawdown)
        or not 0.0 < policy.maximum_drawdown <= 1.0
    ):
        raise ValueError("最大回撤门槛必须在 0 到 1 之间")
    if (
        isinstance(policy.maximum_annualized_volatility, bool)
        or not isinstance(policy.maximum_annualized_volatility, (int, float))
        or not math.isfinite(policy.maximum_annualized_volatility)
        or policy.maximum_annualized_volatility <= 0.0
    ):
        raise ValueError("年化波动率门槛必须是有限正数")
    if (
        type(policy.maximum_consecutive_losing_days) is not int
        or policy.maximum_consecutive_losing_days < 0
    ):
        raise ValueError("最大连续亏损日必须是非负整数")


def _validated_daily(daily: pd.DataFrame) -> pd.DataFrame:
    missing = RISK_DAILY_COLUMNS.difference(daily.columns)
    if missing:
        raise ValueError("风险估值缺少字段：" + ", ".join(sorted(missing)))
    if daily.empty:
        raise ValueError("风险估值至少需要一条记录")
    frame = daily.copy()
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    if dates.isna().any():
        raise ValueError("交易日期必须是有效日期")
    if dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError("交易日期必须严格递增")
    frame["trade_date"] = dates
    frame["equity"] = _finite_series(frame["equity"], "账户权益")
    if (frame["equity"] <= 0.0).any():
        raise ValueError("账户权益必须是有限正数")
    frame["close_price"] = _finite_series(frame["close_price"], "账户收盘价")
    if (frame["close_price"] <= 0.0).any():
        raise ValueError("账户收盘价必须是有限正数")
    shares = _finite_series(frame["shares"], "账户份额")
    if (shares < 0.0).any() or not shares.map(float.is_integer).all():
        raise ValueError("账户份额必须是非负整数")
    frame["shares"] = shares
    recorded_drawdown = _finite_series(frame["drawdown"], "账本回撤")
    expected_drawdown = frame["equity"] / frame["equity"].cummax() - 1.0
    if not all(
        math.isclose(
            actual,
            expected,
            rel_tol=0.0,
            abs_tol=RISK_RECONCILIATION_TOLERANCE,
        )
        for actual, expected in zip(recorded_drawdown, expected_drawdown, strict=True)
    ):
        raise ValueError("账本回撤与权益序列不一致")
    frame["drawdown"] = recorded_drawdown
    exposure = frame["shares"] * frame["close_price"] / frame["equity"]
    if (
        (exposure < -RISK_RECONCILIATION_TOLERANCE).any()
        or (exposure > 1.0 + RISK_RECONCILIATION_TOLERANCE).any()
    ):
        raise ValueError("账户敞口必须在 0 到 1 之间")
    return frame


def _finite_series(series: pd.Series, label: str) -> pd.Series:
    values: list[float] = []
    for value in series.tolist():
        if isinstance(value, bool):
            raise ValueError(f"{label}必须是有限数字")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须是有限数字") from exc
        if not math.isfinite(number):
            if label in {"账户权益", "账户收盘价"}:
                raise ValueError(f"{label}必须是有限正数")
            raise ValueError(f"{label}必须是有限数字")
        values.append(number)
    return pd.Series(values, index=series.index, dtype="float64")


def _longest_true_streak(values: list[bool]) -> int:
    longest = 0
    current = 0
    for value in values:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return longest
