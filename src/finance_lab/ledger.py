from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import BacktestMetrics, calculate_metrics
from finance_lab.validation import assert_valid_daily_prices


@dataclass(frozen=True)
class LedgerConfig:
    initial_cash: float = 100_000.0
    lot_size: int = 100
    commission_bps: float = 3.0
    minimum_commission: float = 5.0
    sell_tax_bps: float = 0.0
    slippage_bps: float = 2.0

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class LedgerChecks:
    invalid_signal_order: int
    negative_cash_rows: int
    negative_share_rows: int
    lot_size_mismatches: int
    cash_reconciliation_mismatches: int
    equity_reconciliation_mismatches: int
    non_finite_rows: int
    non_positive_equity_rows: int

    @property
    def passed(self) -> bool:
        return all(value == 0 for value in asdict(self).values())

    def to_dict(self) -> dict[str, int | bool]:
        return {**asdict(self), "passed": self.passed}


@dataclass(frozen=True)
class LedgerDataHealth:
    manifest_generated_at: str
    as_of_date: date
    manifest_health_status: str
    file_health_status: str
    start_date: date | None
    end_date: date | None
    sources: dict[str, int]
    business_days_stale: int | None
    warning_codes: tuple[str, ...]
    upstream_errors: dict[str, str]
    update_summary_end: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "as_of_date": self.as_of_date.isoformat(),
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
        }


@dataclass(frozen=True)
class _LedgerSimulation:
    frame: pd.DataFrame
    trades: pd.DataFrame
    metrics: BacktestMetrics
    checks: LedgerChecks


@dataclass(frozen=True)
class AccountLedgerResult:
    experiment_id: str
    data_fingerprint: str
    dataset_id: str | None
    curated_file_sha256: str | None
    data_health: LedgerDataHealth | None
    symbol: str
    instrument_kind: str
    strategy: str
    short_window: int
    long_window: int
    config: LedgerConfig
    frame: pd.DataFrame
    trades: pd.DataFrame
    metrics: BacktestMetrics
    checks: LedgerChecks
    benchmark_frame: pd.DataFrame
    benchmark_metrics: BacktestMetrics
    benchmark_checks: LedgerChecks

    @property
    def final_equity(self) -> float:
        return float(self.frame["next_open_equity"].iloc[-1])

    @property
    def benchmark_final_equity(self) -> float:
        return float(self.benchmark_frame["next_open_equity"].iloc[-1])

    def settings_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "engine_version": __version__,
            "data_fingerprint_sha256": self.data_fingerprint,
            "dataset_id": self.dataset_id,
            "curated_file_sha256": self.curated_file_sha256,
            "data_health": self.data_health.to_dict() if self.data_health else None,
            "symbol": self.symbol,
            "instrument_kind": self.instrument_kind,
            "strategy": self.strategy,
            "short_window": self.short_window,
            "long_window": self.long_window,
            "ledger_config": self.config.to_dict(),
        }


def validate_ledger_config(config: LedgerConfig) -> None:
    finite_nonnegative = {
        "commission_bps": config.commission_bps,
        "minimum_commission": config.minimum_commission,
        "sell_tax_bps": config.sell_tax_bps,
        "slippage_bps": config.slippage_bps,
    }
    if not math.isfinite(config.initial_cash) or config.initial_cash <= 0:
        raise ValueError("initial_cash 必须是大于0的有限数字")
    if type(config.lot_size) is not int or config.lot_size <= 0:
        raise ValueError("lot_size 必须是大于0的整数")
    for name, value in finite_nonnegative.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} 必须是大于等于0的有限数字")
    if config.commission_bps >= 10_000 or config.sell_tax_bps >= 10_000:
        raise ValueError("费用比例必须小于10000 bps")
    if config.slippage_bps >= 10_000:
        raise ValueError("slippage_bps 必须小于10000")


def _data_fingerprint(prices: pd.DataFrame) -> str:
    columns = [
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "adjustment",
        "source",
        "volume_unit",
        "ingested_at",
    ]
    canonical = prices[columns].sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    hashed = pd.util.hash_pandas_object(canonical, index=False).to_numpy().tobytes()
    return hashlib.sha256(hashed).hexdigest()


def _config_hash(
    strategy: str,
    short_window: int,
    long_window: int,
    config: LedgerConfig,
) -> str:
    canonical = json.dumps(
        {
            "strategy": strategy,
            "short_window": short_window,
            "long_window": long_window,
            "config": config.to_dict(),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:12]


def _experiment_id(
    strategy: str,
    short_window: int,
    long_window: int,
    config: LedgerConfig,
    fingerprint: str,
    dataset_id: str | None,
    curated_file_sha256: str | None,
    data_health: LedgerDataHealth | None,
) -> str:
    version = __version__.replace(".", "")
    data_health_identity = data_health.to_dict() if data_health else None
    if data_health_identity:
        data_health_identity.pop("manifest_generated_at")
    identity = json.dumps(
        {
            "data_fingerprint": fingerprint,
            "dataset_id": dataset_id,
            "curated_file_sha256": curated_file_sha256,
            "data_health": data_health_identity,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity_hash = hashlib.sha256(identity.encode("ascii")).hexdigest()[:8]
    return (
        f"v{version}_acct_{strategy}_s{short_window}_l{long_window}_"
        f"g{_config_hash(strategy, short_window, long_window, config)}_{identity_hash}"
    )


def commission_for(notional: float, config: LedgerConfig) -> float:
    if notional <= 0:
        return 0.0
    return max(config.minimum_commission, notional * config.commission_bps / 10_000.0)


def affordable_shares(cash: float, execution_price: float, config: LedgerConfig) -> int:
    if cash <= config.minimum_commission or execution_price <= 0:
        return 0
    lots = int(cash // (execution_price * config.lot_size))
    while lots > 0:
        shares = lots * config.lot_size
        notional = shares * execution_price
        if notional + commission_for(notional, config) <= cash:
            return shares
        lots -= 1
    return 0


def _build_signal(
    frame: pd.DataFrame,
    strategy: str,
    short_window: int,
    long_window: int,
) -> pd.DataFrame:
    result = frame.copy()
    result["sma_short"] = result["close"].rolling(short_window).mean()
    result["sma_long"] = result["close"].rolling(long_window).mean()
    if strategy == "sma":
        result["signal"] = (result["sma_short"] > result["sma_long"]).astype(float)
    else:
        result["signal"] = 1.0
    result["signal_date"] = result["trade_date"].shift(1)
    result["decision_signal"] = result["signal"].shift(1).fillna(0.0)
    result["next_open"] = result["open"].shift(-1)
    result["return_end_date"] = result["trade_date"].shift(-1)
    return result[result["return_end_date"].notna()].reset_index(drop=True)


def _numeric_mismatch(left: pd.Series, right: pd.Series) -> int:
    values = left.astype(float) - right.astype(float)
    return int((~np.isfinite(values) | (values.abs() > 1e-8)).sum())


def _simulate(
    prices: pd.DataFrame,
    strategy: str,
    short_window: int,
    long_window: int,
    config: LedgerConfig,
) -> _LedgerSimulation:
    frame = _build_signal(prices, strategy, short_window, long_window)
    cash = float(config.initial_cash)
    shares = 0
    records: list[dict[str, object]] = []
    for row in frame.itertuples(index=False):
        cash_before = cash
        shares_before = shares
        raw_open = float(row.open)
        target = int(float(row.decision_signal) > 0.5)
        action = ""
        filled = False
        execution_price = 0.0
        quantity = 0
        notional = 0.0
        commission = 0.0
        sell_tax = 0.0
        reason = ""
        if target == 1 and shares == 0:
            action = "BUY"
            execution_price = raw_open * (1.0 + config.slippage_bps / 10_000.0)
            quantity = affordable_shares(cash, execution_price, config)
            if quantity > 0:
                notional = quantity * execution_price
                commission = commission_for(notional, config)
                cash -= notional + commission
                shares += quantity
                filled = True
            else:
                reason = "INSUFFICIENT_CASH_FOR_ONE_LOT"
        elif target == 0 and shares > 0:
            action = "SELL"
            execution_price = raw_open * (1.0 - config.slippage_bps / 10_000.0)
            quantity = shares
            notional = quantity * execution_price
            commission = commission_for(notional, config)
            sell_tax = notional * config.sell_tax_bps / 10_000.0
            cash += notional - commission - sell_tax
            shares = 0
            filled = True

        next_open_equity = cash + shares * float(row.next_open)
        pre_trade_equity = cash_before + shares_before * raw_open
        net_return = next_open_equity / pre_trade_equity - 1.0
        records.append(
            {
                **row._asdict(),
                "target_position": target,
                "action": action,
                "filled": filled,
                "reason": reason,
                "execution_price": execution_price,
                "quantity": quantity,
                "notional": notional,
                "commission": commission,
                "sell_tax": sell_tax,
                "total_fees": commission + sell_tax,
                "cash_before": cash_before,
                "cash": cash,
                "shares_before": shares_before,
                "shares": shares,
                "pre_trade_equity": pre_trade_equity,
                "next_open_equity": next_open_equity,
                "net_return": net_return,
                "position": float(shares > 0),
                "turnover": float(filled),
            }
        )
    ledger = pd.DataFrame(records)
    ledger["equity"] = ledger["next_open_equity"] / config.initial_cash
    ledger["drawdown"] = ledger["equity"] / ledger["equity"].cummax() - 1.0
    cash_expected = np.where(
        ledger["action"] == "BUY",
        ledger["cash_before"] - ledger["notional"] - ledger["total_fees"],
        np.where(
            ledger["action"] == "SELL",
            ledger["cash_before"] + ledger["notional"] - ledger["total_fees"],
            ledger["cash_before"],
        ),
    )
    equity_expected = ledger["cash"] + ledger["shares"] * ledger["next_open"]
    numeric_columns = [
        "cash",
        "shares",
        "next_open_equity",
        "net_return",
        "equity",
    ]
    attempted = ledger["action"] != ""
    checks = LedgerChecks(
        invalid_signal_order=int(
            (
                attempted
                & (
                    ledger["signal_date"].isna()
                    | (ledger["signal_date"] >= ledger["trade_date"])
                )
            ).sum()
        ),
        negative_cash_rows=int((ledger["cash"] < -1e-8).sum()),
        negative_share_rows=int((ledger["shares"] < 0).sum()),
        lot_size_mismatches=int((ledger["shares"].astype(int) % config.lot_size != 0).sum()),
        cash_reconciliation_mismatches=_numeric_mismatch(
            ledger["cash"], pd.Series(cash_expected, index=ledger.index)
        ),
        equity_reconciliation_mismatches=_numeric_mismatch(
            ledger["next_open_equity"], equity_expected
        ),
        non_finite_rows=int(
            (~np.isfinite(ledger[numeric_columns].to_numpy(dtype=float))).any(axis=1).sum()
        ),
        non_positive_equity_rows=int((ledger["next_open_equity"] <= 0).sum()),
    )
    metrics = calculate_metrics(
        ledger["net_return"],
        ledger["equity"],
        ledger["position"],
        ledger["turnover"],
    )
    trade_columns = [
        "trade_date",
        "return_end_date",
        "signal_date",
        "action",
        "execution_price",
        "quantity",
        "notional",
        "commission",
        "sell_tax",
        "total_fees",
        "cash_before",
        "cash",
        "shares_before",
        "shares",
    ]
    trades = ledger.loc[ledger["filled"], trade_columns].copy()
    trades = trades.rename(columns={"cash": "cash_after", "shares": "shares_after"})
    return _LedgerSimulation(
        frame=ledger,
        trades=trades.reset_index(drop=True),
        metrics=metrics,
        checks=checks,
    )


def run_account_ledger(
    prices: pd.DataFrame,
    instrument_kind: str,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    config: LedgerConfig | None = None,
    dataset_id: str | None = None,
    curated_file_sha256: str | None = None,
    data_health: LedgerDataHealth | None = None,
) -> AccountLedgerResult:
    assert_valid_daily_prices(prices)
    effective_config = config or LedgerConfig()
    validate_ledger_config(effective_config)
    if instrument_kind == "index":
        raise ValueError("指数本身不可直接交易，账户账本只能用于可交易标的")
    if instrument_kind not in {"etf", "stock"}:
        raise ValueError("instrument_kind 必须是 etf 或 stock")
    if strategy not in {"sma", "buy_hold"}:
        raise ValueError("strategy 只能是 sma 或 buy_hold")
    symbols = tuple(str(value) for value in prices["symbol"].dropna().unique())
    if len(symbols) != 1 or prices["symbol"].isna().any():
        raise ValueError("账户账本要求全部数据属于同一个非空标的")
    if type(short_window) is not int or type(long_window) is not int:
        raise ValueError("均线窗口必须是整数")
    if short_window <= 0 or short_window >= long_window:
        raise ValueError("均线窗口必须满足 0 < short_window < long_window")
    if strategy == "sma" and len(prices) < long_window + 2:
        raise ValueError(f"SMA账户回测至少需要 {long_window + 2} 行数据")
    if len(prices) < 3:
        raise ValueError("账户回测至少需要3行数据，才能形成并估值一笔延迟成交")
    adjustments = set(str(value) for value in prices["adjustment"].dropna().unique())
    if adjustments != {"none"}:
        raise ValueError("账户成交账本要求使用未复权原始成交价格 adjustment=none")

    strategy_run = _simulate(
        prices.sort_values("trade_date").reset_index(drop=True),
        strategy,
        short_window,
        long_window,
        effective_config,
    )
    benchmark_run = _simulate(
        prices.sort_values("trade_date").reset_index(drop=True),
        "buy_hold",
        short_window,
        long_window,
        effective_config,
    )
    if not strategy_run.checks.passed or not benchmark_run.checks.passed:
        raise RuntimeError("账户账本对账检查失败")
    fingerprint = _data_fingerprint(prices)
    return AccountLedgerResult(
        experiment_id=_experiment_id(
            strategy,
            short_window,
            long_window,
            effective_config,
            fingerprint,
            dataset_id,
            curated_file_sha256,
            data_health,
        ),
        data_fingerprint=fingerprint,
        dataset_id=dataset_id,
        curated_file_sha256=curated_file_sha256,
        data_health=data_health,
        symbol=str(prices["symbol"].iloc[0]),
        instrument_kind=instrument_kind,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        config=effective_config,
        frame=strategy_run.frame,
        trades=strategy_run.trades,
        metrics=strategy_run.metrics,
        checks=strategy_run.checks,
        benchmark_frame=benchmark_run.frame,
        benchmark_metrics=benchmark_run.metrics,
        benchmark_checks=benchmark_run.checks,
    )
