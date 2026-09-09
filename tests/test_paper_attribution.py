from __future__ import annotations

import json

import pandas as pd
import pytest

from finance_lab.paper_attribution import build_daily_attribution


def _event(
    trade_date: str,
    sequence_no: int,
    event_type: str,
    *,
    cash: float,
    shares: int,
    close: float,
    equity: float,
    action: str | None = None,
    quantity: int = 0,
    reference_price: float | None = None,
    commission: float = 0.0,
    tax: float = 0.0,
    slippage_cost: float = 0.0,
    notional: float = 0.0,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "account_id": "account-1",
        "trade_date": trade_date,
        "sequence_no": sequence_no,
        "event_type": event_type,
        "action": action,
        "quantity": quantity,
        "reference_price": reference_price,
        "commission": commission,
        "tax": tax,
        "slippage_cost": slippage_cost,
        "notional": notional,
        "cash_after": cash,
        "shares_after": shares,
        "close_price": close,
        "equity_after": equity,
        "payload_json": json.dumps(
            payload or {},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _prices(*rows: tuple[str, float, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "account_id": "account-1",
                "trade_date": trade_date,
                "open_price": open_price,
                "verified_close": close_price,
            }
            for trade_date, open_price, close_price in rows
        ]
    )


def test_daily_attribution_reconciles_a_pure_holding_day() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=10,
                close=10.0,
                equity=1_100.0,
                payload={"open_price": 10.0},
            ),
            _event(
                "2026-01-05",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=10,
                close=12.0,
                equity=1_120.0,
                payload={"open_price": 11.0},
            ),
        ]
    )
    prices = _prices(
        ("2026-01-02", 10.0, 10.0),
        ("2026-01-05", 11.0, 12.0),
    )

    attribution = build_daily_attribution(events, prices)

    baseline = attribution.iloc[0]
    day = attribution.iloc[1]
    assert bool(baseline["is_baseline"])
    assert baseline["equity_change"] == pytest.approx(0.0)
    assert day["equity_change"] == pytest.approx(20.0)
    assert day["overnight_pnl"] == pytest.approx(10.0)
    assert day["existing_position_intraday_pnl"] == pytest.approx(10.0)
    assert day["trade_timing_pnl"] == pytest.approx(0.0)
    assert day["transaction_cost_contribution"] == pytest.approx(0.0)
    assert day["explained_change"] == pytest.approx(20.0)
    assert day["residual"] == pytest.approx(0.0, abs=1e-10)


def test_daily_attribution_separates_buy_timing_and_transaction_costs() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=0,
                close=10.0,
                equity=1_000.0,
                payload={"open_price": 10.0},
            ),
            _event(
                "2026-01-05",
                1,
                "ORDER_FILLED",
                cash=888.0,
                shares=10,
                close=12.0,
                equity=1_008.0,
                action="BUY",
                quantity=10,
                reference_price=11.0,
                commission=1.0,
                slippage_cost=1.0,
                notional=111.0,
            ),
            _event(
                "2026-01-05",
                2,
                "VALUATION",
                cash=888.0,
                shares=10,
                close=12.0,
                equity=1_008.0,
                payload={"open_price": 11.0},
            ),
        ]
    )
    prices = _prices(
        ("2026-01-02", 10.0, 10.0),
        ("2026-01-05", 11.0, 12.0),
    )

    day = build_daily_attribution(events, prices).iloc[1]

    assert day["trade_timing_pnl"] == pytest.approx(10.0)
    assert day["commission_cost"] == pytest.approx(1.0)
    assert day["slippage_cost"] == pytest.approx(1.0)
    assert day["transaction_cost_contribution"] == pytest.approx(-2.0)
    assert day["equity_change"] == pytest.approx(8.0)
    assert day["explained_change"] == pytest.approx(8.0)
    assert day["residual"] == pytest.approx(0.0, abs=1e-10)


def test_daily_attribution_handles_sell_timing_tax_and_slippage() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=10,
                close=10.0,
                equity=1_100.0,
                payload={"open_price": 10.0},
            ),
            _event(
                "2026-01-05",
                1,
                "ORDER_FILLED",
                cash=1_107.0,
                shares=0,
                close=12.0,
                equity=1_107.0,
                action="SELL",
                quantity=10,
                reference_price=11.0,
                commission=1.0,
                tax=1.0,
                slippage_cost=1.0,
                notional=109.0,
            ),
            _event(
                "2026-01-05",
                2,
                "VALUATION",
                cash=1_107.0,
                shares=0,
                close=12.0,
                equity=1_107.0,
                payload={"open_price": 11.0},
            ),
        ]
    )
    prices = _prices(
        ("2026-01-02", 10.0, 10.0),
        ("2026-01-05", 11.0, 12.0),
    )

    day = build_daily_attribution(events, prices).iloc[1]

    assert day["overnight_pnl"] == pytest.approx(10.0)
    assert day["existing_position_intraday_pnl"] == pytest.approx(10.0)
    assert day["trade_timing_pnl"] == pytest.approx(-10.0)
    assert day["transaction_cost_contribution"] == pytest.approx(-3.0)
    assert day["equity_change"] == pytest.approx(7.0)
    assert day["residual"] == pytest.approx(0.0, abs=1e-10)


def test_daily_attribution_bridges_share_adjustment_and_cash_distribution() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=0.0,
                shares=100,
                close=10.0,
                equity=1_000.0,
                payload={"open_price": 10.0},
            ),
            _event(
                "2026-01-05",
                1,
                "SHARE_ADJUSTMENT_APPLIED",
                cash=0.0,
                shares=200,
                close=6.0,
                equity=1_200.0,
                quantity=100,
                payload={"shares_before": 100, "shares_after": 200},
            ),
            _event(
                "2026-01-05",
                2,
                "CASH_DISTRIBUTION_PAID",
                cash=20.0,
                shares=200,
                close=6.0,
                equity=1_220.0,
                quantity=100,
                notional=20.0,
            ),
            _event(
                "2026-01-05",
                3,
                "VALUATION",
                cash=20.0,
                shares=200,
                close=6.0,
                equity=1_220.0,
                payload={"open_price": 5.0},
            ),
        ]
    )
    prices = _prices(
        ("2026-01-02", 10.0, 10.0),
        ("2026-01-05", 5.0, 6.0),
    )

    day = build_daily_attribution(events, prices).iloc[1]

    assert day["overnight_pnl"] == pytest.approx(-500.0)
    assert day["share_adjustment_bridge"] == pytest.approx(500.0)
    assert day["shares_after_adjustment"] == 200
    assert day["existing_position_intraday_pnl"] == pytest.approx(200.0)
    assert day["cash_distribution"] == pytest.approx(20.0)
    assert day["equity_change"] == pytest.approx(220.0)
    assert day["explained_change"] == pytest.approx(220.0)
    assert day["residual"] == pytest.approx(0.0, abs=1e-10)


def test_daily_attribution_rejects_unexplained_equity_change() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=0,
                close=10.0,
                equity=1_000.0,
                payload={"open_price": 10.0},
            ),
            _event(
                "2026-01-05",
                1,
                "VALUATION",
                cash=1_001.0,
                shares=0,
                close=10.0,
                equity=1_001.0,
                payload={"open_price": 10.0},
            ),
        ]
    )
    prices = _prices(
        ("2026-01-02", 10.0, 10.0),
        ("2026-01-05", 10.0, 10.0),
    )

    with pytest.raises(ValueError, match="归因残差"):
        build_daily_attribution(events, prices)


def test_daily_attribution_marks_legacy_open_price_fallback() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=0,
                close=10.0,
                equity=1_000.0,
            )
        ]
    )

    attribution = build_daily_attribution(
        events,
        _prices(("2026-01-02", 10.0, 10.0)),
    )

    assert attribution.iloc[0]["open_price_source"] == "daily_prices_legacy_fallback"


def test_daily_attribution_rejects_internally_inconsistent_baseline() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=0,
                close=10.0,
                equity=1_001.0,
                payload={"open_price": 10.0},
            )
        ]
    )

    with pytest.raises(ValueError, match="权益对账"):
        build_daily_attribution(events, _prices(("2026-01-02", 10.0, 10.0)))


def test_daily_attribution_rejects_nonfinite_transaction_cost() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=0,
                close=10.0,
                equity=1_000.0,
                payload={"open_price": 10.0},
            ),
            _event(
                "2026-01-05",
                1,
                "ORDER_FILLED",
                cash=889.0,
                shares=10,
                close=12.0,
                equity=1_009.0,
                action="BUY",
                quantity=10,
                reference_price=11.0,
                commission=float("nan"),
                slippage_cost=1.0,
            ),
            _event(
                "2026-01-05",
                2,
                "VALUATION",
                cash=889.0,
                shares=10,
                close=12.0,
                equity=1_009.0,
                payload={"open_price": 11.0},
            ),
        ]
    )
    prices = _prices(
        ("2026-01-02", 10.0, 10.0),
        ("2026-01-05", 11.0, 12.0),
    )

    with pytest.raises(ValueError, match="佣金.*有限"):
        build_daily_attribution(events, prices)


def test_daily_attribution_rejects_duplicate_valuation_for_same_account_date() -> None:
    valuation = _event(
        "2026-01-02",
        1,
        "VALUATION",
        cash=1_000.0,
        shares=0,
        close=10.0,
        equity=1_000.0,
        payload={"open_price": 10.0},
    )
    duplicate = {**valuation, "sequence_no": 2}

    with pytest.raises(ValueError, match="每天只能有一条估值"):
        build_daily_attribution(
            pd.DataFrame([valuation, duplicate]),
            _prices(("2026-01-02", 10.0, 10.0)),
        )


def test_daily_attribution_rejects_missing_event_schema() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=0,
                close=10.0,
                equity=1_000.0,
                payload={"open_price": 10.0},
            )
        ]
    ).drop(columns="commission")

    with pytest.raises(ValueError, match="归因事件缺少字段.*commission"):
        build_daily_attribution(events, _prices(("2026-01-02", 10.0, 10.0)))


def test_daily_attribution_rejects_action_date_without_valuation() -> None:
    events = pd.DataFrame(
        [
            _event(
                "2026-01-02",
                1,
                "VALUATION",
                cash=1_000.0,
                shares=0,
                close=10.0,
                equity=1_000.0,
                payload={"open_price": 10.0},
            ),
            _event(
                "2026-01-05",
                1,
                "ORDER_FILLED",
                cash=900.0,
                shares=10,
                close=10.0,
                equity=1_000.0,
                action="BUY",
                quantity=10,
                reference_price=10.0,
            ),
        ]
    )

    with pytest.raises(ValueError, match="账本动作日期缺少估值"):
        build_daily_attribution(events, _prices(("2026-01-02", 10.0, 10.0)))
