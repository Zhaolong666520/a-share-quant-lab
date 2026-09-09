"""Same-period benchmark scorecards for forward paper-trading accounts."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

BENCHMARK_DAILY_COLUMNS = {"account_id", "trade_date", "close_price"}


@dataclass(frozen=True)
class ForwardPriceBenchmark:
    kind: str
    start_date: str
    end_date: str
    valuation_observations: int
    return_observations: int
    start_close: float
    end_close: float
    total_return: float
    annualized_volatility: float | None
    max_drawdown: float
    includes_cash_distributions: bool
    includes_share_adjustments: bool


@dataclass(frozen=True)
class ForwardRelativePerformance:
    account_total_return: float
    cash_benchmark_return: float
    return_difference_vs_cash: float
    asset_price_benchmark_return: float
    return_difference_vs_asset_price: float
    beat_cash: bool
    beat_asset_price: bool
    gate_status: str
    gate_passed: bool


def calculate_shared_price_benchmark(daily: pd.DataFrame) -> ForwardPriceBenchmark:
    """Calculate the shared unadjusted close-price path for paper accounts."""
    frame = _validated_daily(daily)
    first_account = str(frame["account_id"].iloc[0])
    reference = frame.loc[frame["account_id"] == first_account].sort_values("trade_date")
    dates = reference["trade_date"]
    closes = reference["close_price"]
    for account_id in frame["account_id"].drop_duplicates():
        candidate = frame.loc[frame["account_id"] == account_id].sort_values(
            "trade_date"
        )
        candidate_dates = candidate["trade_date"]
        candidate_closes = candidate["close_price"]
        same_dates = len(candidate) == len(reference) and candidate_dates.reset_index(
            drop=True
        ).equals(dates.reset_index(drop=True))
        same_closes = len(candidate) == len(reference) and all(
            math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-10)
            for actual, expected in zip(candidate_closes, closes, strict=True)
        )
        if not same_dates or not same_closes:
            raise ValueError("所有账户必须共享完全相同的估值日期与收盘价")
    returns = closes.pct_change(fill_method=None).dropna()
    drawdown = closes / closes.cummax() - 1.0
    return ForwardPriceBenchmark(
        kind="unadjusted_close_price_only",
        start_date=dates.iloc[0].date().isoformat(),
        end_date=dates.iloc[-1].date().isoformat(),
        valuation_observations=len(reference),
        return_observations=len(returns),
        start_close=float(closes.iloc[0]),
        end_close=float(closes.iloc[-1]),
        total_return=float(closes.iloc[-1] / closes.iloc[0] - 1.0),
        annualized_volatility=(
            float(returns.std(ddof=0) * math.sqrt(252))
            if len(returns) >= 2
            else None
        ),
        max_drawdown=float(drawdown.min()),
        includes_cash_distributions=False,
        includes_share_adjustments=False,
    )


def calculate_relative_performance(
    account_daily: pd.DataFrame,
    *,
    initial_cash: float,
    benchmark: ForwardPriceBenchmark,
    risk_gate_status: str,
) -> ForwardRelativePerformance:
    """Compare one paper account with cash and the shared price-only benchmark."""
    if account_daily.empty:
        raise ValueError("相对表现至少需要一条账户估值")
    relative_columns = BENCHMARK_DAILY_COLUMNS | {"equity"}
    missing = relative_columns.difference(account_daily.columns)
    if missing:
        raise ValueError("相对表现缺少字段：" + ", ".join(sorted(missing)))
    if account_daily["account_id"].astype(str).nunique() != 1:
        raise ValueError("相对表现必须只包含一个账户")
    if (
        isinstance(initial_cash, bool)
        or not isinstance(initial_cash, (int, float))
        or not math.isfinite(initial_cash)
        or initial_cash <= 0.0
    ):
        raise ValueError("初始资金必须是有限正数")
    equities: list[float] = []
    for value in account_daily["equity"]:
        if isinstance(value, bool):
            raise ValueError("账户权益必须是有限正数")
        try:
            equity = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("账户权益必须是有限正数") from exc
        if not math.isfinite(equity) or equity <= 0.0:
            raise ValueError("账户权益必须是有限正数")
        equities.append(equity)
    frame = account_daily.copy()
    frame["equity"] = pd.Series(equities, index=frame.index, dtype="float64")
    account_benchmark = calculate_shared_price_benchmark(frame)
    if account_benchmark != benchmark:
        raise ValueError("账户价格路径与共享基准不一致")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.sort_values("trade_date")
    if not math.isclose(
        float(frame["equity"].iloc[0]),
        initial_cash,
        rel_tol=0.0,
        abs_tol=1e-8,
    ):
        raise ValueError("首日权益必须等于初始资金")
    allowed_risk_statuses = {
        "insufficient_history",
        "risk_limit_breached",
        "extended_paper_observation",
    }
    if risk_gate_status not in allowed_risk_statuses:
        raise ValueError("风险门禁状态无效")
    account_total_return = float(frame["equity"].iloc[-1]) / initial_cash - 1.0
    price_difference = account_total_return - benchmark.total_return
    beat_cash = account_total_return > 0.0
    beat_asset_price = price_difference > 0.0
    if risk_gate_status != "extended_paper_observation":
        gate_status = risk_gate_status
    elif not beat_cash:
        gate_status = "did_not_beat_cash"
    elif not beat_asset_price:
        gate_status = "did_not_beat_asset_price"
    else:
        gate_status = "extended_paper_observation"
    gate_passed = gate_status == "extended_paper_observation"
    return ForwardRelativePerformance(
        account_total_return=account_total_return,
        cash_benchmark_return=0.0,
        return_difference_vs_cash=account_total_return,
        asset_price_benchmark_return=benchmark.total_return,
        return_difference_vs_asset_price=price_difference,
        beat_cash=beat_cash,
        beat_asset_price=beat_asset_price,
        gate_status=gate_status,
        gate_passed=gate_passed,
    )


def _validated_daily(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        raise ValueError("前向基准至少需要一条估值")
    missing = BENCHMARK_DAILY_COLUMNS.difference(daily.columns)
    if missing:
        raise ValueError("前向基准缺少字段：" + ", ".join(sorted(missing)))
    frame = daily.copy()
    frame["account_id"] = frame["account_id"].astype(str)
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    if dates.isna().any():
        raise ValueError("前向基准交易日期无效")
    frame["trade_date"] = dates
    if frame.duplicated(["account_id", "trade_date"]).any():
        raise ValueError("同一账户的估值日期不能重复")
    closes: list[float] = []
    for value in frame["close_price"]:
        if isinstance(value, bool):
            raise ValueError("基准收盘价必须是有限正数")
        try:
            close = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("基准收盘价必须是有限正数") from exc
        if not math.isfinite(close) or close <= 0.0:
            raise ValueError("基准收盘价必须是有限正数")
        closes.append(close)
    frame["close_price"] = pd.Series(closes, index=frame.index, dtype="float64")
    return frame
