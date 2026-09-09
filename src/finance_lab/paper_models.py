from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from finance_lab import __version__
from finance_lab.ledger import LedgerConfig, validate_ledger_config

PaperStrategyName = Literal["sma", "momentum"]
PaperAction = Literal["BUY", "SELL"]
PaperOperationStatus = Literal["initialized", "processed", "no-op", "status"]
MAX_ORDER_ATTEMPTS = 3


@dataclass(frozen=True)
class StrategySpec:
    name: PaperStrategyName
    short_window: int | None = None
    long_window: int | None = None
    momentum_lookback: int | None = None


@dataclass(frozen=True)
class PaperAccount:
    account_id: str
    portfolio_id: str
    symbol: str
    instrument_kind: str
    strategy: StrategySpec
    ledger_config: LedgerConfig
    created_market_date: date
    engine_version: str
    config_hash: str

    @property
    def initial_cash(self) -> float:
        return self.ledger_config.initial_cash


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    signal_date: date
    action: PaperAction
    attempt_count: int = 0


@dataclass(frozen=True)
class CashDistributionEntitlement:
    action_id: str
    record_date: date
    payment_date: date
    cash_per_share: float
    entitled_shares: int
    cash_amount: float
    source_url: str


@dataclass(frozen=True)
class PaperState:
    account_id: str
    last_trade_date: date
    cash: float
    shares: int
    last_close: float
    equity: float
    equity_peak: float
    drawdown: float
    last_target_position: int | None
    pending_order: PendingOrder | None
    last_event_hash: str
    distribution_entitlements: tuple[CashDistributionEntitlement, ...] = ()


@dataclass(frozen=True)
class PaperEventDraft:
    event_type: str
    trade_date: date
    signal_date: date | None
    order_id: str | None
    action: PaperAction | None
    quantity: int
    reference_price: float | None
    execution_price: float | None
    notional: float
    commission: float
    tax: float
    slippage_cost: float
    cash_after: float
    shares_after: int
    close_price: float
    equity_after: float
    drawdown_after: float
    reason_code: str | None
    payload: dict[str, object]


@dataclass(frozen=True)
class EngineStep:
    state: PaperState
    events: tuple[PaperEventDraft, ...]


@dataclass(frozen=True)
class PaperDataContext:
    dataset_id: str
    curated_sha256: str
    manifest_generated_at: str
    as_of_date: date
    data_start_date: date
    data_end_date: date
    sources: dict[str, int]
    warning_codes: tuple[str, ...]
    upstream_errors: dict[str, str]
    business_days_stale: int
    update_summary_end: str


@dataclass(frozen=True)
class PaperOperationResult:
    status: PaperOperationStatus
    portfolio_id: str
    processed_dates: tuple[date, ...]
    states: tuple[PaperState, ...]
    data_context: PaperDataContext | None
    report_path: Path | None = None


def validate_portfolio_id(value: str) -> str:
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is None:
        raise ValueError("账户组名称只能包含小写字母、数字和单个连字符")
    return value


def _account_config_hash(
    symbol: str,
    instrument_kind: str,
    strategy: StrategySpec,
    ledger_config: LedgerConfig,
) -> str:
    canonical = json.dumps(
        {
            "symbol": symbol,
            "instrument_kind": instrument_kind,
            "strategy": asdict(strategy),
            "ledger_config": ledger_config.to_dict(),
            "engine_version": __version__,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def make_default_accounts(
    portfolio_id: str,
    created_market_date: date,
    ledger_config: LedgerConfig,
    symbol: str = "sh.510300",
    instrument_kind: str = "etf",
) -> tuple[PaperAccount, PaperAccount]:
    safe_portfolio = validate_portfolio_id(portfolio_id)
    validate_ledger_config(ledger_config)
    strategies = (
        StrategySpec(name="sma", short_window=20, long_window=60),
        StrategySpec(name="momentum", momentum_lookback=120),
    )
    account_suffixes = ("sma-20-60-v1", "momentum-120-v1")
    accounts = tuple(
        PaperAccount(
            account_id=f"{safe_portfolio}-{suffix}",
            portfolio_id=safe_portfolio,
            symbol=symbol,
            instrument_kind=instrument_kind,
            strategy=strategy,
            ledger_config=ledger_config,
            created_market_date=created_market_date,
            engine_version=__version__,
            config_hash=_account_config_hash(
                symbol,
                instrument_kind,
                strategy,
                ledger_config,
            ),
        )
        for suffix, strategy in zip(account_suffixes, strategies, strict=True)
    )
    return accounts[0], accounts[1]
