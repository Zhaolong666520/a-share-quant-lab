from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from finance_lab.ledger import LedgerConfig
from finance_lab.paper_engine import advance_one_bar, initialize_account, signal_for_history
from finance_lab.paper_models import StrategySpec, make_default_accounts


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
