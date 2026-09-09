from __future__ import annotations

import math
from dataclasses import replace

import pandas as pd
import pytest

from finance_lab.paper_risk import (
    DEFAULT_PAPER_RISK_POLICY,
    PaperRiskPolicy,
    calculate_paper_risk,
)


def _daily(
    equities: list[float],
    *,
    shares: list[int] | None = None,
    closes: list[float] | None = None,
) -> pd.DataFrame:
    count = len(equities)
    return pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2026-01-02", periods=count),
            "equity": equities,
            "shares": shares or [0] * count,
            "close_price": closes or [10.0] * count,
            "drawdown": pd.Series(equities) / pd.Series(equities).cummax() - 1.0,
        }
    )


def _changed(
    daily: pd.DataFrame,
    column: str,
    row: int,
    value: object,
) -> pd.DataFrame:
    changed = daily.copy()
    changed[column] = changed[column].astype(object)
    changed.loc[row, column] = value
    return changed


def test_risk_snapshot_marks_short_history_without_hiding_observed_risk() -> None:
    equities = [100.0, 101.0, 99.99, 98.9901, 99.4850505]
    daily = _daily(equities, shares=[0, 5, 5, 0, 0])
    expected_volatility = float(
        pd.Series([0.01, -0.01, -0.01, 0.005]).std(ddof=0) * math.sqrt(252)
    )

    snapshot = calculate_paper_risk(daily)

    assert snapshot.valuation_observations == 5
    assert snapshot.return_observations == 4
    assert snapshot.annualized_volatility == pytest.approx(expected_volatility)
    assert snapshot.max_drawdown == pytest.approx(-0.0199)
    assert snapshot.current_drawdown == pytest.approx(99.4850505 / 101.0 - 1.0)
    assert snapshot.worst_daily_return == pytest.approx(-0.01)
    assert snapshot.losing_days == 2
    assert snapshot.max_consecutive_losing_days == 2
    assert snapshot.average_exposure == pytest.approx((50 / 101 + 50 / 99.99) / 5)
    assert snapshot.current_exposure == pytest.approx(0.0)
    assert snapshot.breach_codes == ()
    assert snapshot.gate_status == "insufficient_history"
    assert not snapshot.gate_passed


def test_risk_snapshot_fails_the_paper_gate_when_a_fixed_limit_is_breached() -> None:
    policy = PaperRiskPolicy(
        minimum_return_observations=3,
        maximum_drawdown=0.05,
        maximum_annualized_volatility=10.0,
        maximum_consecutive_losing_days=10,
    )

    snapshot = calculate_paper_risk(_daily([100.0, 100.0, 90.0, 90.0]), policy)

    assert snapshot.breach_codes == ("MAX_DRAWDOWN",)
    assert snapshot.gate_status == "risk_limit_breached"
    assert not snapshot.gate_passed


def test_risk_snapshot_reports_volatility_and_losing_streak_breaches() -> None:
    policy = PaperRiskPolicy(
        minimum_return_observations=3,
        maximum_drawdown=0.99,
        maximum_annualized_volatility=0.0001,
        maximum_consecutive_losing_days=1,
    )

    snapshot = calculate_paper_risk(_daily([100.0, 99.0, 98.0, 97.0]), policy)

    assert snapshot.breach_codes == (
        "ANNUALIZED_VOLATILITY",
        "LOSING_STREAK",
    )
    assert snapshot.gate_status == "risk_limit_breached"


def test_risk_snapshot_only_passes_after_the_fixed_history_minimum() -> None:
    snapshot = calculate_paper_risk(_daily([100.0] * 61))

    assert snapshot.return_observations == 60
    assert snapshot.breach_codes == ()
    assert snapshot.gate_status == "extended_paper_observation"
    assert snapshot.gate_passed


@pytest.mark.parametrize(
    ("daily", "message"),
    [
        (_daily([100.0]).drop(columns="equity"), "风险估值缺少字段.*equity"),
        (_daily([]), "风险估值至少需要一条记录"),
        (
            _changed(_daily([100.0, 101.0]), "trade_date", 1, pd.Timestamp("2026-01-02")),
            "交易日期必须严格递增",
        ),
        (
            _changed(_daily([100.0, 101.0]), "equity", 1, float("nan")),
            "账户权益必须是有限正数",
        ),
        (
            _changed(_daily([100.0, 101.0]), "shares", 1, 0.5),
            "账户份额必须是非负整数",
        ),
        (
            _changed(_daily([100.0, 101.0]), "close_price", 1, 0.0),
            "账户收盘价必须是有限正数",
        ),
        (
            _changed(_daily([100.0, 90.0]), "drawdown", 1, 0.0),
            "账本回撤与权益序列不一致",
        ),
        (
            _daily([100.0], shares=[11], closes=[10.0]),
            "账户敞口必须在 0 到 1 之间",
        ),
    ],
)
def test_risk_snapshot_rejects_invalid_valuation_history(
    daily: pd.DataFrame,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        calculate_paper_risk(daily)


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (
            replace(DEFAULT_PAPER_RISK_POLICY, minimum_return_observations=0),
            "最少收益观察数必须是至少为 2 的整数",
        ),
        (
            replace(DEFAULT_PAPER_RISK_POLICY, minimum_return_observations=1),
            "最少收益观察数必须是至少为 2 的整数",
        ),
        (
            replace(DEFAULT_PAPER_RISK_POLICY, maximum_drawdown=float("nan")),
            "最大回撤门槛必须在 0 到 1 之间",
        ),
        (
            replace(DEFAULT_PAPER_RISK_POLICY, maximum_drawdown=1.1),
            "最大回撤门槛必须在 0 到 1 之间",
        ),
        (
            replace(DEFAULT_PAPER_RISK_POLICY, maximum_annualized_volatility=0.0),
            "年化波动率门槛必须是有限正数",
        ),
        (
            replace(DEFAULT_PAPER_RISK_POLICY, maximum_consecutive_losing_days=-1),
            "最大连续亏损日必须是非负整数",
        ),
    ],
)
def test_risk_snapshot_rejects_invalid_policy(
    policy: PaperRiskPolicy,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        calculate_paper_risk(_daily([100.0]), policy)
