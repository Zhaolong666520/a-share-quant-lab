from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pandas as pd
import pytest

from finance_lab.cash_distributions import CashDistribution
from finance_lab.ledger import LedgerConfig
from finance_lab.paper_engine import advance_one_bar, initialize_account, signal_for_history
from finance_lab.paper_models import PaperState, PendingOrder, StrategySpec, make_default_accounts
from finance_lab.share_adjustments import ShareAdjustment


def test_signal_warmup_is_not_silently_treated_as_cash() -> None:
    assert signal_for_history(pd.Series([100.0] * 59), StrategySpec("sma", 20, 60)) is None
    assert (
        signal_for_history(
            pd.Series([100.0] * 120),
            StrategySpec("momentum", momentum_lookback=120),
        )
        is None
    )


def test_future_prices_do_not_change_today_signal() -> None:
    prefix = pd.Series([float(value) for value in range(1, 122)])

    first = signal_for_history(prefix, StrategySpec("momentum", momentum_lookback=120))
    second = signal_for_history(
        pd.concat([prefix, pd.Series([9999.0])]).iloc[:-1],
        StrategySpec("momentum", momentum_lookback=120),
    )

    assert first == second == 1


@pytest.mark.parametrize("closes", [pd.Series([0.0]), pd.Series([float("nan")])])
def test_signal_rejects_non_positive_or_non_finite_close(closes: pd.Series) -> None:
    with pytest.raises(ValueError, match="有限正数"):
        signal_for_history(closes, StrategySpec("momentum", momentum_lookback=1))


def _history(closes: list[float]) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-02", periods=len(closes))
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": closes,
            "close": closes,
        }
    )


def _momentum_account(created_market_date: date, config: LedgerConfig):
    return make_default_accounts("default", created_market_date, config)[1]


def test_initial_signal_creates_order_filled_at_next_bar_open() -> None:
    initial_history = _history([float(value) for value in range(100, 221)])
    account = _momentum_account(
        initial_history.iloc[-1]["trade_date"].date(),
        LedgerConfig(commission_bps=0.0, minimum_commission=0.0, slippage_bps=0.0),
    )

    created = initialize_account(account, initial_history)
    advanced_history = _history([float(value) for value in range(100, 222)])
    advanced = advance_one_bar(account, created.state, advanced_history)

    assert [event.event_type for event in created.events] == [
        "ACCOUNT_CREATED",
        "VALUATION",
        "SIGNAL_GENERATED",
        "ORDER_CREATED",
    ]
    assert created.state.pending_order is not None
    assert created.state.pending_order.signal_date == created.state.last_trade_date
    assert advanced.events[0].event_type == "ORDER_FILLED"
    assert advanced.events[0].signal_date is not None
    assert advanced.events[0].signal_date < advanced.events[0].trade_date
    assert advanced.state.shares % account.ledger_config.lot_size == 0
    assert advanced.state.cash >= 0.0
    assert advanced.state.equity == pytest.approx(
        advanced.state.cash
        + advanced.state.shares * float(advanced_history.iloc[-1]["close"])
    )


def test_insufficient_cash_skips_once_without_recreating_same_buy_order() -> None:
    initial_history = _history([float(value) for value in range(100, 221)])
    account = _momentum_account(
        initial_history.iloc[-1]["trade_date"].date(),
        LedgerConfig(
            initial_cash=1_000.0,
            commission_bps=0.0,
            minimum_commission=0.0,
            slippage_bps=0.0,
        ),
    )

    created = initialize_account(account, initial_history)
    skipped = advance_one_bar(account, created.state, _history(list(range(100, 222))))
    repeated = advance_one_bar(account, skipped.state, _history(list(range(100, 223))))

    assert [event.event_type for event in skipped.events][:1] == ["ORDER_SKIPPED"]
    assert skipped.state.pending_order is None
    assert skipped.state.last_target_position == 1
    assert "ORDER_CREATED" not in [event.event_type for event in repeated.events]
    assert "ORDER_SKIPPED" not in [event.event_type for event in repeated.events]


def test_buy_fill_applies_slippage_and_commission_once() -> None:
    initial_history = _history([float(value) for value in range(100, 221)])
    account = _momentum_account(
        initial_history.iloc[-1]["trade_date"].date(),
        LedgerConfig(commission_bps=10.0, minimum_commission=5.0, slippage_bps=2.0),
    )

    created = initialize_account(account, initial_history)
    advanced_history = _history([float(value) for value in range(100, 222)])
    advanced = advance_one_bar(account, created.state, advanced_history)
    fill = advanced.events[0]

    assert fill.execution_price is not None
    assert fill.execution_price == pytest.approx(float(advanced_history.iloc[-1]["open"]) * 1.0002)
    assert fill.commission == pytest.approx(max(5.0, fill.notional * 0.001))
    assert fill.slippage_cost == pytest.approx(
        fill.quantity * (fill.execution_price - float(advanced_history.iloc[-1]["open"]))
    )
    assert advanced.state.cash == pytest.approx(100_000.0 - fill.notional - fill.commission)


def test_invalid_current_open_rejects_bar_without_state_transition() -> None:
    initial_history = _history([float(value) for value in range(100, 221)])
    account = _momentum_account(
        initial_history.iloc[-1]["trade_date"].date(),
        LedgerConfig(),
    )
    created = initialize_account(account, initial_history)
    invalid = _history([float(value) for value in range(100, 222)])
    invalid.loc[invalid.index[-1], "open"] = 0.0

    with pytest.raises(ValueError, match="开盘价"):
        advance_one_bar(account, created.state, invalid)


def test_target_reversal_sells_entire_position_at_following_open() -> None:
    initial_history = _history([float(value) for value in range(100, 221)])
    account = _momentum_account(
        initial_history.iloc[-1]["trade_date"].date(),
        LedgerConfig(commission_bps=0.0, minimum_commission=0.0, slippage_bps=0.0),
    )

    created = initialize_account(account, initial_history)
    bought = advance_one_bar(account, created.state, _history(list(range(100, 222))))
    sell_signal = advance_one_bar(
        account,
        bought.state,
        _history([*range(100, 222), 50]),
    )
    sold = advance_one_bar(
        account,
        sell_signal.state,
        _history([*range(100, 222), 50, 49]),
    )

    assert sell_signal.state.pending_order is not None
    assert sell_signal.state.pending_order.action == "SELL"
    assert sold.events[0].event_type == "ORDER_FILLED"
    assert sold.events[0].action == "SELL"
    assert sold.events[0].signal_date is not None
    assert sold.events[0].signal_date < sold.events[0].trade_date
    assert sold.state.shares == 0
    assert sold.state.cash > 0.0


def test_sell_fill_charges_commission_tax_and_slippage_once() -> None:
    initial_history = _history([float(value) for value in range(100, 221)])
    account = _momentum_account(
        initial_history.iloc[-1]["trade_date"].date(),
        LedgerConfig(
            commission_bps=10.0,
            minimum_commission=5.0,
            sell_tax_bps=10.0,
            slippage_bps=2.0,
        ),
    )

    created = initialize_account(account, initial_history)
    bought = advance_one_bar(account, created.state, _history(list(range(100, 222))))
    sell_signal = advance_one_bar(
        account,
        bought.state,
        _history([*range(100, 222), 50]),
    )
    sold_history = _history([*range(100, 222), 50, 49])
    sold = advance_one_bar(account, sell_signal.state, sold_history)
    fill = sold.events[0]

    assert fill.action == "SELL"
    assert fill.execution_price is not None
    assert fill.execution_price == pytest.approx(49.0 * 0.9998)
    assert fill.tax == pytest.approx(fill.notional * 0.001)
    assert fill.slippage_cost == pytest.approx(fill.quantity * (49.0 - fill.execution_price))
    assert sold.state.cash == pytest.approx(
        bought.state.cash + fill.notional - fill.commission - fill.tax
    )


def test_cash_distribution_uses_record_date_shares_and_pays_after_open_order() -> None:
    record_date = date(2026, 1, 19)
    payment_date = date(2026, 1, 22)
    account = _momentum_account(
        date(2026, 1, 16),
        LedgerConfig(commission_bps=0.0, minimum_commission=0.0, slippage_bps=0.0),
    )
    state = PaperState(
        account_id=account.account_id,
        last_trade_date=date(2026, 1, 16),
        cash=1_000.0,
        shares=100,
        last_close=10.0,
        equity=2_000.0,
        equity_peak=2_000.0,
        drawdown=0.0,
        last_target_position=1,
        pending_order=None,
        last_event_hash=account.config_hash,
    )
    distribution = CashDistribution(
        action_id="a" * 64,
        symbol="sh.510300",
        record_date=record_date,
        ex_date=date(2026, 1, 20),
        payment_date=payment_date,
        cash_per_share=0.5,
        currency="CNY",
        source_url="https://example.test/notices/510300-2026-01",
        source_published_at=datetime.fromisoformat("2026-01-15T08:00:00+08:00"),
        ingested_at=datetime.fromisoformat("2026-01-15T09:00:00+08:00"),
    )
    record_history = pd.DataFrame(
        {
            "trade_date": [date(2026, 1, 16), record_date],
            "open": [10.0, 10.0],
            "close": [10.0, 10.0],
        }
    )

    recorded = advance_one_bar(
        account,
        state,
        record_history,
        cash_distributions=(distribution,),
    )

    assert [event.event_type for event in recorded.events[:2]] == [
        "VALUATION",
        "CASH_DISTRIBUTION_ENTITLED",
    ]
    assert recorded.events[1].quantity == 100
    assert recorded.events[1].notional == pytest.approx(50.0)
    assert recorded.state.cash == pytest.approx(1_000.0)
    assert recorded.state.distribution_entitlements[0].action_id == distribution.action_id

    sell_order = PendingOrder(
        order_id="sell-before-distribution-payment",
        signal_date=date(2026, 1, 21),
        action="SELL",
    )
    ready_to_sell = replace(recorded.state, pending_order=sell_order)
    payment_history = pd.DataFrame(
        {
            "trade_date": [date(2026, 1, 16), record_date, payment_date],
            "open": [10.0, 10.0, 10.0],
            "close": [10.0, 10.0, 10.0],
        }
    )

    paid = advance_one_bar(
        account,
        ready_to_sell,
        payment_history,
        cash_distributions=(distribution,),
    )

    assert [event.event_type for event in paid.events[:2]] == [
        "ORDER_FILLED",
        "CASH_DISTRIBUTION_PAID",
    ]
    assert paid.events[1].quantity == 100
    assert paid.events[1].notional == pytest.approx(50.0)
    assert paid.state.shares == 0
    assert paid.state.cash == pytest.approx(2_050.0)
    assert paid.state.distribution_entitlements == ()


def test_cash_distribution_records_zero_share_observation() -> None:
    previous_date = date(2026, 1, 16)
    record_date = date(2026, 1, 19)
    account = _momentum_account(previous_date, LedgerConfig())
    state = PaperState(
        account_id=account.account_id,
        last_trade_date=previous_date,
        cash=2_000.0,
        shares=0,
        last_close=10.0,
        equity=2_000.0,
        equity_peak=2_000.0,
        drawdown=0.0,
        last_target_position=0,
        pending_order=None,
        last_event_hash=account.config_hash,
    )
    distribution = CashDistribution(
        action_id="zero-share-action",
        symbol=account.symbol,
        record_date=record_date,
        ex_date=record_date,
        payment_date=date(2026, 1, 22),
        cash_per_share=0.5,
        currency="CNY",
        source_url="https://example.test/distributions/zero-share",
        source_published_at=datetime.fromisoformat("2026-01-15T08:00:00+08:00"),
        ingested_at=datetime.fromisoformat("2026-01-15T09:00:00+08:00"),
    )
    history = pd.DataFrame(
        {
            "trade_date": [previous_date, record_date],
            "open": [10.0, 10.0],
            "close": [10.0, 10.0],
        }
    )

    result = advance_one_bar(
        account,
        state,
        history,
        cash_distributions=(distribution,),
    )

    assert [event.event_type for event in result.events[:2]] == [
        "VALUATION",
        "CASH_DISTRIBUTION_NOT_ENTITLED",
    ]
    assert result.events[1].quantity == 0
    assert result.events[1].notional == 0.0
    assert result.state.distribution_entitlements == ()


def test_share_adjustment_applies_before_pending_open_sell() -> None:
    previous_date = date(2026, 1, 19)
    effective_date = date(2026, 1, 20)
    account = _momentum_account(
        previous_date,
        LedgerConfig(commission_bps=0.0, minimum_commission=0.0, slippage_bps=0.0),
    )
    state = PaperState(
        account_id=account.account_id,
        last_trade_date=previous_date,
        cash=1_000.0,
        shares=100,
        last_close=10.0,
        equity=2_000.0,
        equity_peak=2_000.0,
        drawdown=0.0,
        last_target_position=0,
        pending_order=PendingOrder(
            order_id="sell-after-adjustment",
            signal_date=previous_date,
            action="SELL",
        ),
        last_event_hash=account.config_hash,
    )
    adjustment = ShareAdjustment(
        action_id="share-adjustment-2026",
        symbol=account.symbol,
        effective_date=effective_date,
        ratio_numerator=3,
        ratio_denominator=2,
        source_url="https://example.test/notices/share-adjustment",
        source_published_at=datetime.fromisoformat("2026-01-15T08:00:00+08:00"),
        ingested_at=datetime.fromisoformat("2026-01-15T09:00:00+08:00"),
    )
    adjusted_price = 20.0 / 3.0
    history = pd.DataFrame(
        {
            "trade_date": [previous_date, effective_date],
            "open": [10.0, adjusted_price],
            "close": [10.0, adjusted_price],
        }
    )

    result = advance_one_bar(
        account,
        state,
        history,
        share_adjustments=(adjustment,),
    )

    assert [event.event_type for event in result.events[:2]] == [
        "SHARE_ADJUSTMENT_APPLIED",
        "ORDER_FILLED",
    ]
    assert result.events[0].quantity == 50
    assert result.events[0].shares_after == 150
    assert result.events[1].quantity == 150
    assert result.state.shares == 0
    assert result.state.cash == pytest.approx(2_000.0)


def test_share_adjustment_rejects_fractional_account_result() -> None:
    previous_date = date(2026, 1, 19)
    effective_date = date(2026, 1, 20)
    account = _momentum_account(previous_date, LedgerConfig())
    state = PaperState(
        account_id=account.account_id,
        last_trade_date=previous_date,
        cash=1_000.0,
        shares=100,
        last_close=10.0,
        equity=2_000.0,
        equity_peak=2_000.0,
        drawdown=0.0,
        last_target_position=1,
        pending_order=None,
        last_event_hash=account.config_hash,
    )
    adjustment = ShareAdjustment(
        action_id="fractional-adjustment",
        symbol=account.symbol,
        effective_date=effective_date,
        ratio_numerator=4,
        ratio_denominator=3,
        source_url="https://example.test/notices/fractional-adjustment",
        source_published_at=datetime.fromisoformat("2026-01-15T08:00:00+08:00"),
        ingested_at=datetime.fromisoformat("2026-01-15T09:00:00+08:00"),
    )
    history = pd.DataFrame(
        {
            "trade_date": [previous_date, effective_date],
            "open": [10.0, 7.5],
            "close": [10.0, 7.5],
        }
    )

    with pytest.raises(ValueError, match="零碎份额"):
        advance_one_bar(
            account,
            state,
            history,
            share_adjustments=(adjustment,),
        )


def test_share_adjustment_does_not_scale_effective_date_open_buy() -> None:
    previous_date = date(2026, 1, 19)
    effective_date = date(2026, 1, 20)
    account = _momentum_account(
        previous_date,
        LedgerConfig(commission_bps=0.0, minimum_commission=0.0, slippage_bps=0.0),
    )
    state = PaperState(
        account_id=account.account_id,
        last_trade_date=previous_date,
        cash=1_000.0,
        shares=0,
        last_close=10.0,
        equity=1_000.0,
        equity_peak=1_000.0,
        drawdown=0.0,
        last_target_position=1,
        pending_order=PendingOrder(
            order_id="buy-after-adjustment",
            signal_date=previous_date,
            action="BUY",
        ),
        last_event_hash=account.config_hash,
    )
    adjustment = ShareAdjustment(
        action_id="zero-share-adjustment",
        symbol=account.symbol,
        effective_date=effective_date,
        ratio_numerator=2,
        ratio_denominator=1,
        source_url="https://example.test/notices/zero-share-adjustment",
        source_published_at=datetime.fromisoformat("2026-01-15T08:00:00+08:00"),
        ingested_at=datetime.fromisoformat("2026-01-15T09:00:00+08:00"),
    )
    history = pd.DataFrame(
        {
            "trade_date": [previous_date, effective_date],
            "open": [10.0, 5.0],
            "close": [10.0, 5.0],
        }
    )

    result = advance_one_bar(
        account,
        state,
        history,
        share_adjustments=(adjustment,),
    )

    assert [event.event_type for event in result.events[:2]] == [
        "SHARE_ADJUSTMENT_NOT_APPLICABLE",
        "ORDER_FILLED",
    ]
    assert result.events[0].reason_code == "NO_SHARES_BEFORE_EFFECTIVE_OPEN"
    assert result.events[1].quantity == 200
    assert result.state.shares == 200
