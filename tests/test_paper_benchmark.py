from __future__ import annotations

import math

import pandas as pd
import pytest

from finance_lab.paper_benchmark import (
    calculate_relative_performance,
    calculate_shared_price_benchmark,
)


def _shared_daily(
    closes: list[float],
    *,
    accounts: tuple[str, ...] = ("account-a", "account-b"),
) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=len(closes))
    return pd.DataFrame(
        [
            {
                "account_id": account_id,
                "trade_date": trade_date,
                "close_price": close,
            }
            for trade_date, close in zip(dates, closes, strict=True)
            for account_id in accounts
        ]
    )


def test_shared_price_benchmark_uses_the_exact_forward_valuation_path() -> None:
    daily = _shared_daily([10.0, 11.0, 9.9])

    benchmark = calculate_shared_price_benchmark(daily)

    assert benchmark.kind == "unadjusted_close_price_only"
    assert benchmark.start_date == "2026-01-02"
    assert benchmark.end_date == "2026-01-06"
    assert benchmark.valuation_observations == 3
    assert benchmark.return_observations == 2
    assert benchmark.start_close == pytest.approx(10.0)
    assert benchmark.end_close == pytest.approx(9.9)
    assert benchmark.total_return == pytest.approx(-0.01)
    assert benchmark.annualized_volatility == pytest.approx(0.1 * math.sqrt(252))
    assert benchmark.max_drawdown == pytest.approx(-0.10)
    assert not benchmark.includes_cash_distributions
    assert not benchmark.includes_share_adjustments


@pytest.mark.parametrize("mutation", ["different_close", "missing_date"])
def test_shared_price_benchmark_rejects_different_account_paths(mutation: str) -> None:
    daily = _shared_daily([10.0, 11.0, 12.0])
    if mutation == "different_close":
        daily.loc[
            (daily["account_id"] == "account-b")
            & (daily["trade_date"] == pd.Timestamp("2026-01-05")),
            "close_price",
        ] = 11.5
    else:
        daily = daily.loc[
            ~(
                (daily["account_id"] == "account-b")
                & (daily["trade_date"] == pd.Timestamp("2026-01-05"))
            )
        ]

    with pytest.raises(ValueError, match="所有账户必须共享完全相同的估值日期与收盘价"):
        calculate_shared_price_benchmark(daily)


@pytest.mark.parametrize(
    ("daily", "message"),
    [
        (
            _shared_daily([10.0]).drop(columns="close_price"),
            "前向基准缺少字段.*close_price",
        ),
        (_shared_daily([]), "前向基准至少需要一条估值"),
        (
            pd.concat(
                [_shared_daily([10.0, 11.0]), _shared_daily([10.0, 11.0]).iloc[[0]]],
                ignore_index=True,
            ),
            "同一账户的估值日期不能重复",
        ),
        (
            _shared_daily([10.0, float("nan")]),
            "基准收盘价必须是有限正数",
        ),
        (_shared_daily([10.0, 0.0]), "基准收盘价必须是有限正数"),
    ],
)
def test_shared_price_benchmark_rejects_invalid_history(
    daily: pd.DataFrame,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        calculate_shared_price_benchmark(daily)


def test_relative_performance_passes_only_as_extended_paper_observation() -> None:
    closes = [100.0 + index / 12 for index in range(61)]
    daily = _shared_daily(closes, accounts=("account-a",))
    daily["equity"] = [100.0 + index / 6 for index in range(61)]
    benchmark = calculate_shared_price_benchmark(daily)

    relative = calculate_relative_performance(
        daily,
        initial_cash=100.0,
        benchmark=benchmark,
        risk_gate_status="extended_paper_observation",
    )

    assert relative.account_total_return == pytest.approx(0.10)
    assert relative.cash_benchmark_return == pytest.approx(0.0)
    assert relative.return_difference_vs_cash == pytest.approx(0.10)
    assert relative.asset_price_benchmark_return == pytest.approx(0.05)
    assert relative.return_difference_vs_asset_price == pytest.approx(0.05)
    assert relative.beat_cash
    assert relative.beat_asset_price
    assert relative.gate_status == "extended_paper_observation"
    assert relative.gate_passed


@pytest.mark.parametrize(
    ("risk_status", "end_close", "end_equity", "expected_status"),
    [
        ("insufficient_history", 90.0, 110.0, "insufficient_history"),
        ("risk_limit_breached", 90.0, 110.0, "risk_limit_breached"),
        ("extended_paper_observation", 90.0, 95.0, "did_not_beat_cash"),
        (
            "extended_paper_observation",
            110.0,
            105.0,
            "did_not_beat_asset_price",
        ),
    ],
)
def test_relative_performance_reports_the_first_failed_evidence_gate(
    risk_status: str,
    end_close: float,
    end_equity: float,
    expected_status: str,
) -> None:
    daily = _shared_daily([100.0, end_close], accounts=("account-a",))
    daily["equity"] = [100.0, end_equity]
    benchmark = calculate_shared_price_benchmark(daily)

    relative = calculate_relative_performance(
        daily,
        initial_cash=100.0,
        benchmark=benchmark,
        risk_gate_status=risk_status,
    )

    assert relative.gate_status == expected_status
    assert not relative.gate_passed


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("empty", "相对表现至少需要一条账户估值"),
        ("missing_equity", "相对表现缺少字段.*equity"),
        ("multiple_accounts", "相对表现必须只包含一个账户"),
        ("bad_initial_cash", "初始资金必须是有限正数"),
        ("nonfinite_equity", "账户权益必须是有限正数"),
        ("wrong_baseline", "首日权益必须等于初始资金"),
        ("different_price_path", "账户价格路径与共享基准不一致"),
        ("invalid_date", "前向基准交易日期无效"),
        ("unknown_risk_status", "风险门禁状态无效"),
    ],
)
def test_relative_performance_rejects_invalid_inputs(
    mutation: str,
    message: str,
) -> None:
    benchmark_daily = _shared_daily([100.0, 110.0], accounts=("account-a",))
    benchmark = calculate_shared_price_benchmark(benchmark_daily)
    account_daily = benchmark_daily.copy()
    account_daily["equity"] = [100.0, 105.0]
    initial_cash = 100.0
    risk_status = "extended_paper_observation"
    if mutation == "empty":
        account_daily = account_daily.iloc[0:0]
    elif mutation == "missing_equity":
        account_daily = account_daily.drop(columns="equity")
    elif mutation == "multiple_accounts":
        account_daily = _shared_daily([100.0, 110.0])
        account_daily["equity"] = [100.0, 100.0, 105.0, 105.0]
    elif mutation == "bad_initial_cash":
        initial_cash = 0.0
    elif mutation == "nonfinite_equity":
        account_daily.loc[1, "equity"] = float("nan")
    elif mutation == "wrong_baseline":
        account_daily.loc[0, "equity"] = 99.0
    elif mutation == "different_price_path":
        account_daily.loc[1, "close_price"] = 109.0
    elif mutation == "invalid_date":
        account_daily["trade_date"] = account_daily["trade_date"].astype(object)
        account_daily.loc[1, "trade_date"] = "not-a-date"
    else:
        risk_status = "unknown"

    with pytest.raises(ValueError, match=message):
        calculate_relative_performance(
            account_daily,
            initial_cash=initial_cash,
            benchmark=benchmark,
            risk_gate_status=risk_status,
        )
