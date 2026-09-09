"""Mechanical daily P&L attribution for the forward paper ledger."""

from __future__ import annotations

import json
import math
from typing import Any, cast

import pandas as pd

ATTRIBUTION_COLUMNS = (
    "account_id",
    "trade_date",
    "is_baseline",
    "open_price_source",
    "prior_close",
    "open_price",
    "close_price",
    "shares_at_prior_close",
    "shares_after_adjustment",
    "shares_at_close",
    "equity_start",
    "equity_end",
    "equity_change",
    "overnight_pnl",
    "existing_position_intraday_pnl",
    "trade_timing_pnl",
    "share_adjustment_bridge",
    "cash_distribution",
    "commission_cost",
    "tax_cost",
    "slippage_cost",
    "transaction_cost_contribution",
    "explained_change",
    "residual",
)
ATTRIBUTION_TOLERANCE = 1e-6
EVENT_COLUMNS = {
    "account_id",
    "trade_date",
    "sequence_no",
    "event_type",
    "action",
    "quantity",
    "reference_price",
    "commission",
    "tax",
    "slippage_cost",
    "notional",
    "cash_after",
    "shares_after",
    "close_price",
    "equity_after",
    "payload_json",
}
PRICE_COLUMNS = {"account_id", "trade_date", "open_price", "verified_close"}


def build_daily_attribution(
    events: pd.DataFrame,
    verified_prices: pd.DataFrame,
) -> pd.DataFrame:
    missing_event_columns = EVENT_COLUMNS.difference(events.columns)
    if missing_event_columns:
        raise ValueError(
            "归因事件缺少字段：" + ", ".join(sorted(missing_event_columns))
        )
    missing_price_columns = PRICE_COLUMNS.difference(verified_prices.columns)
    if missing_price_columns:
        raise ValueError(
            "归因行情缺少字段：" + ", ".join(sorted(missing_price_columns))
        )
    if events.empty:
        raise ValueError("收益归因没有可处理的账本事件")
    event_frame = events.copy()
    price_frame = verified_prices.copy()
    event_frame["trade_date"] = pd.to_datetime(event_frame["trade_date"])
    price_frame["trade_date"] = pd.to_datetime(price_frame["trade_date"])
    valuation_keys = event_frame.loc[
        event_frame["event_type"] == "VALUATION", ["account_id", "trade_date"]
    ]
    if valuation_keys.duplicated().any():
        raise ValueError("同一账户每天只能有一条估值事件")
    valuation_key_set = {
        (str(row.account_id), pd.Timestamp(row.trade_date))
        for row in valuation_keys.itertuples(index=False)
    }
    action_keys = event_frame.loc[
        event_frame["event_type"] != "VALUATION", ["account_id", "trade_date"]
    ].drop_duplicates()
    if any(
        (str(row.account_id), pd.Timestamp(row.trade_date)) not in valuation_key_set
        for row in action_keys.itertuples(index=False)
    ):
        raise ValueError("账本动作日期缺少估值事件")
    rows: list[dict[str, object]] = []
    for account_id, account_events in event_frame.groupby("account_id", sort=True):
        account_events = account_events.sort_values(["trade_date", "sequence_no"])
        valuations = account_events.loc[account_events["event_type"] == "VALUATION"]
        prior: pd.Series | None = None
        for _, valuation in valuations.iterrows():
            trade_date = pd.Timestamp(valuation["trade_date"])
            matching_prices = price_frame.loc[
                (price_frame["account_id"] == account_id)
                & (price_frame["trade_date"] == trade_date)
            ]
            if len(matching_prices) != 1:
                raise ValueError("每个估值日必须恰好匹配一条已验证行情")
            price = matching_prices.iloc[0]
            verified_open = _positive_float(price["open_price"], "已验证开盘价")
            verified_close = _positive_float(price["verified_close"], "已验证收盘价")
            close = _positive_float(valuation["close_price"], "估值收盘价")
            if not math.isclose(close, verified_close, rel_tol=0.0, abs_tol=1e-10):
                raise ValueError("估值收盘价与已验证行情不一致")
            payload = json.loads(str(valuation["payload_json"]))
            event_open = payload.get("open_price")
            open_price = (
                verified_open
                if event_open is None
                else _positive_float(event_open, "估值事件开盘价")
            )
            if not math.isclose(open_price, verified_open, rel_tol=0.0, abs_tol=1e-10):
                raise ValueError("估值开盘价与已验证行情不一致")

            cash_end = _finite_float(valuation["cash_after"], "估值现金")
            equity_end = _finite_float(valuation["equity_after"], "估值权益")
            shares_at_close = _nonnegative_int(valuation["shares_after"], "估值份额")
            if not math.isclose(
                equity_end,
                cash_end + shares_at_close * close,
                rel_tol=0.0,
                abs_tol=1e-8,
            ):
                raise ValueError("估值权益对账失败")
            if prior is None:
                row = {
                    "account_id": str(account_id),
                    "trade_date": trade_date.date().isoformat(),
                    "is_baseline": True,
                    "open_price_source": (
                        "daily_prices_legacy_fallback"
                        if event_open is None
                        else "event_payload"
                    ),
                    "prior_close": close,
                    "open_price": open_price,
                    "close_price": close,
                    "shares_at_prior_close": shares_at_close,
                    "shares_after_adjustment": shares_at_close,
                    "shares_at_close": shares_at_close,
                    "equity_start": equity_end,
                    "equity_end": equity_end,
                    "equity_change": 0.0,
                    "overnight_pnl": 0.0,
                    "existing_position_intraday_pnl": 0.0,
                    "trade_timing_pnl": 0.0,
                    "share_adjustment_bridge": 0.0,
                    "cash_distribution": 0.0,
                    "commission_cost": 0.0,
                    "tax_cost": 0.0,
                    "slippage_cost": 0.0,
                    "transaction_cost_contribution": 0.0,
                    "explained_change": 0.0,
                    "residual": 0.0,
                }
            else:
                prior_close = float(prior["close_price"])
                prior_shares = int(prior["shares_after"])
                equity_start = float(prior["equity_after"])
                overnight_pnl = prior_shares * (open_price - prior_close)
                day_events = account_events.loc[
                    (account_events["trade_date"] == trade_date)
                    & (account_events["sequence_no"] < valuation["sequence_no"])
                ]
                shares_after_adjustment = prior_shares
                adjustments = day_events.loc[
                    day_events["event_type"] == "SHARE_ADJUSTMENT_APPLIED"
                ]
                for _, adjustment in adjustments.iterrows():
                    adjustment_payload = json.loads(str(adjustment["payload_json"]))
                    shares_before = int(adjustment_payload["shares_before"])
                    shares_after = int(adjustment_payload["shares_after"])
                    if shares_before != shares_after_adjustment:
                        raise ValueError("份额调整事件无法衔接前一份额")
                    shares_after_adjustment = shares_after
                share_adjustment_bridge = (
                    shares_after_adjustment - prior_shares
                ) * open_price
                intraday_pnl = shares_after_adjustment * (close - open_price)
                fills = day_events.loc[day_events["event_type"] == "ORDER_FILLED"]
                trade_timing_pnl = 0.0
                expected_close_shares = shares_after_adjustment
                commission_cost = 0.0
                tax_cost = 0.0
                slippage_cost = 0.0
                for _, fill in fills.iterrows():
                    quantity = _positive_int(fill["quantity"], "成交数量")
                    reference_price = _positive_float(
                        fill["reference_price"], "成交参考开盘价"
                    )
                    if not math.isclose(
                        reference_price,
                        open_price,
                        rel_tol=0.0,
                        abs_tol=1e-10,
                    ):
                        raise ValueError("成交参考开盘价与估值开盘价不一致")
                    commission_cost += _nonnegative_float(fill["commission"], "佣金")
                    tax_cost += _nonnegative_float(fill["tax"], "税费")
                    slippage_cost += _nonnegative_float(
                        fill["slippage_cost"], "滑点成本"
                    )
                    if fill["action"] == "BUY":
                        trade_timing_pnl += quantity * (close - open_price)
                        expected_close_shares += quantity
                    elif fill["action"] == "SELL":
                        trade_timing_pnl += quantity * (open_price - close)
                        expected_close_shares -= quantity
                    else:
                        raise ValueError("成交事件的买卖方向无效")
                if expected_close_shares != shares_at_close:
                    raise ValueError("成交与份额调整无法解释估值份额")
                cash_distribution = sum(
                    _positive_float(value, "现金分红金额")
                    for value in day_events.loc[
                        day_events["event_type"] == "CASH_DISTRIBUTION_PAID",
                        "notional",
                    ]
                )
                transaction_cost_contribution = -(
                    commission_cost + tax_cost + slippage_cost
                )
                equity_change = equity_end - equity_start
                explained_change = (
                    overnight_pnl
                    + intraday_pnl
                    + trade_timing_pnl
                    + share_adjustment_bridge
                    + cash_distribution
                    + transaction_cost_contribution
                )
                residual = equity_change - explained_change
                if abs(residual) > ATTRIBUTION_TOLERANCE:
                    raise ValueError(
                        f"{account_id} 在 {trade_date.date().isoformat()} 的归因残差超限："
                        f"{residual:.12f}"
                    )
                row = {
                    "account_id": str(account_id),
                    "trade_date": trade_date.date().isoformat(),
                    "is_baseline": False,
                    "open_price_source": (
                        "daily_prices_legacy_fallback"
                        if event_open is None
                        else "event_payload"
                    ),
                    "prior_close": prior_close,
                    "open_price": open_price,
                    "close_price": close,
                    "shares_at_prior_close": prior_shares,
                    "shares_after_adjustment": shares_after_adjustment,
                    "shares_at_close": shares_at_close,
                    "equity_start": equity_start,
                    "equity_end": equity_end,
                    "equity_change": equity_change,
                    "overnight_pnl": overnight_pnl,
                    "existing_position_intraday_pnl": intraday_pnl,
                    "trade_timing_pnl": trade_timing_pnl,
                    "share_adjustment_bridge": share_adjustment_bridge,
                    "cash_distribution": cash_distribution,
                    "commission_cost": commission_cost,
                    "tax_cost": tax_cost,
                    "slippage_cost": slippage_cost,
                    "transaction_cost_contribution": transaction_cost_contribution,
                    "explained_change": explained_change,
                    "residual": residual,
                }
            rows.append(row)
            prior = valuation
    return pd.DataFrame(rows, columns=ATTRIBUTION_COLUMNS)


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是有限数字")
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是有限数字") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有限数字")
    return number


def _positive_float(value: object, label: str) -> float:
    number = _finite_float(value, label)
    if number <= 0.0:
        raise ValueError(f"{label}必须是正数")
    return number


def _nonnegative_float(value: object, label: str) -> float:
    number = _finite_float(value, label)
    if number < 0.0:
        raise ValueError(f"{label}不能为负数")
    return number


def _nonnegative_int(value: object, label: str) -> int:
    number = _finite_float(value, label)
    if number < 0.0 or not number.is_integer():
        raise ValueError(f"{label}必须是非负整数")
    return int(number)


def _positive_int(value: object, label: str) -> int:
    number = _nonnegative_int(value, label)
    if number == 0:
        raise ValueError(f"{label}必须是正整数")
    return number
