from __future__ import annotations

import hashlib
import math
from datetime import date

import numpy as np
import pandas as pd

from finance_lab.ledger import affordable_shares, commission_for, validate_ledger_config
from finance_lab.paper_models import (
    EngineStep,
    PaperAccount,
    PaperAction,
    PaperEventDraft,
    PaperState,
    PendingOrder,
    StrategySpec,
)


def signal_for_history(closes: pd.Series, spec: StrategySpec) -> int | None:
    """Return the target position derived only from the supplied close history."""
    values = closes.astype(float)
    if values.empty or not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("close 必须全部是有限正数")
    if spec.name == "sma":
        if spec.short_window is None or spec.long_window is None:
            raise ValueError("SMA 策略必须同时设置短期和长期窗口")
        if spec.short_window <= 0 or spec.long_window <= 0 or spec.short_window >= spec.long_window:
            raise ValueError("SMA 窗口必须满足 0 < short < long")
        if len(values) < spec.long_window:
            return None
        return int(
            values.iloc[-spec.short_window :].mean()
            > values.iloc[-spec.long_window :].mean()
        )
    if spec.name == "momentum":
        if spec.momentum_lookback is None or spec.momentum_lookback <= 0:
            raise ValueError("动量策略必须设置正的回看窗口")
        if len(values) <= spec.momentum_lookback:
            return None
        return int(values.iloc[-1] / values.iloc[-1 - spec.momentum_lookback] - 1.0 > 0)
    raise ValueError(f"不支持的模拟盘策略：{spec.name}")


def _validated_history(history: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "close"}
    missing = required.difference(history.columns)
    if missing:
        raise ValueError(f"日线缺少字段：{', '.join(sorted(missing))}")
    frame = history.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    if frame.empty or frame["trade_date"].isna().any():
        raise ValueError("交易日期不能为空")
    if not frame["trade_date"].is_monotonic_increasing:
        raise ValueError("交易日期必须升序")
    if frame["trade_date"].duplicated().any():
        raise ValueError("交易日期不能重复")
    return frame


def _current_bar(history: pd.DataFrame) -> tuple[date, float, float]:
    frame = _validated_history(history)
    if "open" not in frame.columns:
        raise ValueError("日线缺少字段：open")
    row = frame.iloc[-1]
    trade_date = pd.Timestamp(row["trade_date"]).date()
    raw_open = float(row["open"])
    close = float(row["close"])
    if not math.isfinite(raw_open) or raw_open <= 0.0:
        raise ValueError("开盘价必须是有限正数")
    if not math.isfinite(close) or close <= 0.0:
        raise ValueError("收盘价必须是有限正数")
    return trade_date, raw_open, close


def _event(
    event_type: str,
    trade_date: date,
    state: PaperState,
    *,
    signal_date: date | None = None,
    order_id: str | None = None,
    action: PaperAction | None = None,
    quantity: int = 0,
    reference_price: float | None = None,
    execution_price: float | None = None,
    notional: float = 0.0,
    commission: float = 0.0,
    tax: float = 0.0,
    slippage_cost: float = 0.0,
    reason_code: str | None = None,
    payload: dict[str, object] | None = None,
) -> PaperEventDraft:
    return PaperEventDraft(
        event_type=event_type,
        trade_date=trade_date,
        signal_date=signal_date,
        order_id=order_id,
        action=action,
        quantity=quantity,
        reference_price=reference_price,
        execution_price=execution_price,
        notional=notional,
        commission=commission,
        tax=tax,
        slippage_cost=slippage_cost,
        cash_after=state.cash,
        shares_after=state.shares,
        close_price=state.last_close,
        equity_after=state.equity,
        drawdown_after=state.drawdown,
        reason_code=reason_code,
        payload=payload or {},
    )


def _order_id(account_id: str, signal_date: date, action: str) -> str:
    identity = f"{account_id}:{signal_date.isoformat()}:{action}"
    return "paper-" + hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


def _state_after_valuation(
    account: PaperAccount,
    state: PaperState,
    trade_date: date,
    close: float,
    *,
    cash: float | None = None,
    shares: int | None = None,
    pending_order: PendingOrder | None = None,
    last_target_position: int | None = None,
) -> PaperState:
    next_cash = state.cash if cash is None else cash
    next_shares = state.shares if shares is None else shares
    if not math.isfinite(next_cash) or next_cash < -1e-8:
        raise ValueError("现金不能为负或非有限")
    if next_shares < 0 or next_shares % account.ledger_config.lot_size != 0:
        raise ValueError("份额必须是非负整手")
    equity = next_cash + next_shares * close
    if not math.isfinite(equity) or equity <= 0.0:
        raise ValueError("账户权益必须是有限正数")
    equity_peak = max(state.equity_peak, equity)
    drawdown = equity / equity_peak - 1.0
    if not math.isfinite(drawdown):
        raise ValueError("回撤必须是有限数字")
    return PaperState(
        account_id=account.account_id,
        last_trade_date=trade_date,
        cash=max(next_cash, 0.0),
        shares=next_shares,
        last_close=close,
        equity=equity,
        equity_peak=equity_peak,
        drawdown=drawdown,
        last_target_position=last_target_position,
        pending_order=pending_order,
        last_event_hash=state.last_event_hash,
    )


def _initial_state(account: PaperAccount, trade_date: date, close: float) -> PaperState:
    cash = account.initial_cash
    return PaperState(
        account_id=account.account_id,
        last_trade_date=trade_date,
        cash=cash,
        shares=0,
        last_close=close,
        equity=cash,
        equity_peak=cash,
        drawdown=0.0,
        last_target_position=None,
        pending_order=None,
        last_event_hash=account.config_hash,
    )


def _order_for_target(
    account: PaperAccount,
    state: PaperState,
    target: int,
    trade_date: date,
) -> PendingOrder | None:
    if target == 1 and state.last_target_position != 1 and state.shares == 0:
        return PendingOrder(
            order_id=_order_id(account.account_id, trade_date, "BUY"),
            signal_date=trade_date,
            action="BUY",
        )
    if target == 0 and state.last_target_position == 1 and state.shares > 0:
        return PendingOrder(
            order_id=_order_id(account.account_id, trade_date, "SELL"),
            signal_date=trade_date,
            action="SELL",
        )
    return None


def _signal_events(
    account: PaperAccount,
    state: PaperState,
    closes: pd.Series,
    trade_date: date,
) -> tuple[PaperState, tuple[PaperEventDraft, ...]]:
    target = signal_for_history(closes, account.strategy)
    signal_state = _state_after_valuation(
        account,
        state,
        trade_date,
        state.last_close,
        pending_order=None,
        last_target_position=state.last_target_position if target is None else target,
    )
    signal_event = _event(
        "SIGNAL_GENERATED",
        trade_date,
        signal_state,
        signal_date=trade_date,
        reason_code="NOT_READY" if target is None else None,
        payload={"target_position": target},
    )
    if target is None:
        return signal_state, (signal_event,)
    order = _order_for_target(account, state, target, trade_date)
    if order is None:
        return signal_state, (signal_event,)
    ordered_state = _state_after_valuation(
        account,
        signal_state,
        trade_date,
        state.last_close,
        pending_order=order,
        last_target_position=target,
    )
    order_event = _event(
        "ORDER_CREATED",
        trade_date,
        ordered_state,
        signal_date=trade_date,
        order_id=order.order_id,
        action=order.action,
        payload={"target_position": target},
    )
    return ordered_state, (signal_event, order_event)


def initialize_account(account: PaperAccount, history: pd.DataFrame) -> EngineStep:
    """Create the forward-only starting state at the latest available close."""
    validate_ledger_config(account.ledger_config)
    trade_date, _raw_open, close = _current_bar(history)
    if trade_date != account.created_market_date:
        raise ValueError("账户创建日期必须等于初始化行情的最新日期")
    initial_state = _initial_state(account, trade_date, close)
    account_event = _event(
        "ACCOUNT_CREATED",
        trade_date,
        initial_state,
        payload={
            "account_id": account.account_id,
            "config_hash": account.config_hash,
            "strategy": account.strategy.name,
        },
    )
    valuation_event = _event("VALUATION", trade_date, initial_state)
    final_state, signal_events = _signal_events(
        account,
        initial_state,
        _validated_history(history)["close"],
        trade_date,
    )
    return EngineStep(state=final_state, events=(account_event, valuation_event, *signal_events))


def advance_one_bar(account: PaperAccount, state: PaperState, history: pd.DataFrame) -> EngineStep:
    """Fill any prior order at the supplied bar open, then calculate its close signal."""
    validate_ledger_config(account.ledger_config)
    trade_date, raw_open, close = _current_bar(history)
    if state.account_id != account.account_id:
        raise ValueError("账户状态与账户配置不匹配")
    if trade_date <= state.last_trade_date:
        raise ValueError("新行情日期必须晚于账户最后处理日期")

    cash = state.cash
    shares = state.shares
    events: list[PaperEventDraft] = []
    if state.pending_order is not None:
        order = state.pending_order
        if order.action == "BUY":
            execution_price = raw_open * (1.0 + account.ledger_config.slippage_bps / 10_000.0)
            quantity = affordable_shares(cash, execution_price, account.ledger_config)
            if quantity == 0:
                after_order = _state_after_valuation(
                    account,
                    state,
                    trade_date,
                    close,
                    pending_order=None,
                    last_target_position=state.last_target_position,
                )
                events.append(
                    _event(
                        "ORDER_SKIPPED",
                        trade_date,
                        after_order,
                        signal_date=order.signal_date,
                        order_id=order.order_id,
                        action=order.action,
                        reference_price=raw_open,
                        reason_code="INSUFFICIENT_CASH_FOR_ONE_LOT",
                    )
                )
            else:
                notional = quantity * execution_price
                commission = commission_for(notional, account.ledger_config)
                cash -= notional + commission
                shares += quantity
                after_order = _state_after_valuation(
                    account,
                    state,
                    trade_date,
                    close,
                    cash=cash,
                    shares=shares,
                    pending_order=None,
                    last_target_position=state.last_target_position,
                )
                events.append(
                    _event(
                        "ORDER_FILLED",
                        trade_date,
                        after_order,
                        signal_date=order.signal_date,
                        order_id=order.order_id,
                        action=order.action,
                        quantity=quantity,
                        reference_price=raw_open,
                        execution_price=execution_price,
                        notional=notional,
                        commission=commission,
                        slippage_cost=quantity * (execution_price - raw_open),
                    )
                )
        else:
            quantity = shares
            execution_price = raw_open * (1.0 - account.ledger_config.slippage_bps / 10_000.0)
            if quantity == 0:
                after_order = _state_after_valuation(
                    account,
                    state,
                    trade_date,
                    close,
                    pending_order=None,
                    last_target_position=state.last_target_position,
                )
                events.append(
                    _event(
                        "ORDER_SKIPPED",
                        trade_date,
                        after_order,
                        signal_date=order.signal_date,
                        order_id=order.order_id,
                        action=order.action,
                        reference_price=raw_open,
                        reason_code="NO_SHARES_TO_SELL",
                    )
                )
            else:
                notional = quantity * execution_price
                commission = commission_for(notional, account.ledger_config)
                tax = notional * account.ledger_config.sell_tax_bps / 10_000.0
                cash += notional - commission - tax
                shares = 0
                after_order = _state_after_valuation(
                    account,
                    state,
                    trade_date,
                    close,
                    cash=cash,
                    shares=shares,
                    pending_order=None,
                    last_target_position=state.last_target_position,
                )
                events.append(
                    _event(
                        "ORDER_FILLED",
                        trade_date,
                        after_order,
                        signal_date=order.signal_date,
                        order_id=order.order_id,
                        action=order.action,
                        quantity=quantity,
                        reference_price=raw_open,
                        execution_price=execution_price,
                        notional=notional,
                        commission=commission,
                        tax=tax,
                        slippage_cost=quantity * (raw_open - execution_price),
                    )
                )
        state_after_order = after_order
    else:
        state_after_order = _state_after_valuation(
            account,
            state,
            trade_date,
            close,
            pending_order=None,
            last_target_position=state.last_target_position,
        )

    valuation_event = _event("VALUATION", trade_date, state_after_order)
    final_state, signal_events = _signal_events(
        account,
        state_after_order,
        _validated_history(history)["close"],
        trade_date,
    )
    return EngineStep(state=final_state, events=(*events, valuation_event, *signal_events))
