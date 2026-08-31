"""Durable local storage for the forward paper-trading ledger."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Self, TypedDict, cast

import duckdb

from finance_lab.ledger import LedgerConfig, validate_ledger_config
from finance_lab.paper_models import (
    EngineStep,
    PaperAccount,
    PaperAction,
    PaperDataContext,
    PaperEventDraft,
    PaperState,
    PaperStrategyName,
    PendingOrder,
    StrategySpec,
)


class PaperStoreError(RuntimeError):
    """Base error for durable paper-trading ledger operations."""


class PaperPortfolioExistsError(PaperStoreError):
    """Raised when initialization would overwrite an existing portfolio."""


class PaperIdempotencyError(PaperStoreError):
    """Raised when an account/date has already been committed."""


class PaperAuditError(PaperStoreError):
    """Raised when immutable events and the derived state cache disagree."""


class PaperAccountSnapshot(TypedDict):
    account_id: str
    symbol: str
    instrument_kind: str
    strategy: str
    strategy_parameters: dict[str, object]
    ledger_config: dict[str, object]
    config_hash: str
    created_market_date: str
    engine_version: str
    state: dict[str, object]
    latest_run: dict[str, object]


class PaperPortfolioSnapshot(TypedDict):
    portfolio_id: str
    accounts: list[PaperAccountSnapshot]


class PaperStore:
    """Own the isolated paper-trading tables inside a local DuckDB database."""

    def __init__(self, database: Path, read_only: bool = False) -> None:
        self.database = database
        self.read_only = read_only
        self._connection: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> Self:
        self._connection = duckdb.connect(str(self.database), read_only=self.read_only)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def ensure_schema(self) -> None:
        """Create schema version 1 without touching existing market-data tables."""
        if self.read_only:
            raise RuntimeError("只读账本不能创建或升级表结构")
        connection = self._require_connection()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_schema_metadata (
                schema_version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_accounts (
                account_id VARCHAR PRIMARY KEY,
                portfolio_id VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                instrument_kind VARCHAR NOT NULL,
                strategy VARCHAR NOT NULL,
                strategy_parameters_json VARCHAR NOT NULL,
                initial_cash DOUBLE NOT NULL CHECK (initial_cash > 0),
                lot_size BIGINT NOT NULL CHECK (lot_size > 0),
                commission_bps DOUBLE NOT NULL CHECK (commission_bps >= 0),
                minimum_commission DOUBLE NOT NULL CHECK (minimum_commission >= 0),
                sell_tax_bps DOUBLE NOT NULL CHECK (sell_tax_bps >= 0),
                slippage_bps DOUBLE NOT NULL CHECK (slippage_bps >= 0),
                config_hash VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_market_date DATE NOT NULL,
                engine_version VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_runs (
                run_id VARCHAR PRIMARY KEY,
                batch_id VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                status VARCHAR NOT NULL CHECK (status = 'committed'),
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP NOT NULL,
                dataset_id VARCHAR NOT NULL,
                curated_sha256 VARCHAR NOT NULL,
                manifest_generated_at VARCHAR NOT NULL,
                data_health_json VARCHAR NOT NULL,
                event_count INTEGER NOT NULL CHECK (event_count >= 0),
                state_hash VARCHAR NOT NULL,
                UNIQUE (account_id, trade_date)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_events (
                event_id VARCHAR PRIMARY KEY,
                run_id VARCHAR NOT NULL,
                account_id VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
                event_type VARCHAR NOT NULL,
                signal_date DATE,
                order_id VARCHAR,
                action VARCHAR,
                quantity BIGINT NOT NULL CHECK (quantity >= 0),
                reference_price DOUBLE,
                execution_price DOUBLE,
                notional DOUBLE NOT NULL CHECK (notional >= 0),
                commission DOUBLE NOT NULL CHECK (commission >= 0),
                tax DOUBLE NOT NULL CHECK (tax >= 0),
                slippage_cost DOUBLE NOT NULL CHECK (slippage_cost >= 0),
                cash_after DOUBLE NOT NULL CHECK (cash_after >= 0),
                shares_after BIGINT NOT NULL CHECK (shares_after >= 0),
                close_price DOUBLE NOT NULL,
                equity_after DOUBLE NOT NULL,
                drawdown_after DOUBLE NOT NULL,
                reason_code VARCHAR,
                payload_json VARCHAR NOT NULL,
                previous_event_hash VARCHAR NOT NULL,
                event_hash VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (run_id, sequence_no)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_state (
                account_id VARCHAR PRIMARY KEY,
                last_trade_date DATE NOT NULL,
                cash DOUBLE NOT NULL CHECK (cash >= 0),
                shares BIGINT NOT NULL CHECK (shares >= 0),
                last_close DOUBLE NOT NULL,
                equity DOUBLE NOT NULL,
                equity_peak DOUBLE NOT NULL,
                drawdown DOUBLE NOT NULL,
                last_target_position INTEGER,
                pending_order_json VARCHAR,
                last_event_hash VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO paper_schema_metadata (schema_version) VALUES (?) "
            "ON CONFLICT (schema_version) DO NOTHING",
            [1],
        )

    def portfolio_exists(self, portfolio_id: str) -> bool:
        connection = self._require_connection()
        result = connection.execute(
            "SELECT 1 FROM paper_accounts WHERE portfolio_id = ? LIMIT 1",
            [portfolio_id],
        ).fetchone()
        return result is not None

    def initialize_portfolio(
        self,
        accounts: Sequence[PaperAccount],
        steps: Sequence[EngineStep],
        data_context: PaperDataContext,
        *,
        batch_id: str,
    ) -> tuple[PaperState, ...]:
        """Write immutable accounts and their first event batch atomically."""
        self._validate_batch(accounts, steps)
        portfolio_ids = {account.portfolio_id for account in accounts}
        if len(portfolio_ids) != 1:
            raise PaperStoreError("初始化批次中的账户必须属于同一个账户组")
        portfolio_id = next(iter(portfolio_ids))
        connection = self._require_connection()

        connection.execute("BEGIN TRANSACTION")
        try:
            if self.portfolio_exists(portfolio_id):
                raise PaperPortfolioExistsError(f"账户组 {portfolio_id} 已存在，不能覆盖")
            for account in accounts:
                self._insert_account(account)
            states = self._append_batch(accounts, steps, data_context, batch_id)
            connection.execute("COMMIT")
            return states
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def commit_batch(
        self,
        accounts: Sequence[PaperAccount],
        steps: Sequence[EngineStep],
        data_context: PaperDataContext,
        *,
        batch_id: str,
    ) -> tuple[PaperState, ...]:
        """Append one validated trade-date batch for all supplied accounts atomically."""
        self._validate_batch(accounts, steps)
        connection = self._require_connection()

        connection.execute("BEGIN TRANSACTION")
        try:
            self._assert_existing_accounts_match(accounts)
            states = self._append_batch(accounts, steps, data_context, batch_id)
            connection.execute("COMMIT")
            return states
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _insert_account(self, account: PaperAccount) -> None:
        self._require_connection().execute(
            """
            INSERT INTO paper_accounts (
                account_id, portfolio_id, symbol, instrument_kind, strategy,
                strategy_parameters_json, initial_cash, lot_size, commission_bps,
                minimum_commission, sell_tax_bps, slippage_bps, config_hash,
                created_market_date, engine_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                account.account_id,
                account.portfolio_id,
                account.symbol,
                account.instrument_kind,
                account.strategy.name,
                _canonical_json(asdict(account.strategy)),
                account.initial_cash,
                account.ledger_config.lot_size,
                account.ledger_config.commission_bps,
                account.ledger_config.minimum_commission,
                account.ledger_config.sell_tax_bps,
                account.ledger_config.slippage_bps,
                account.config_hash,
                account.created_market_date,
                account.engine_version,
            ],
        )

    def _append_batch(
        self,
        accounts: Sequence[PaperAccount],
        steps: Sequence[EngineStep],
        data_context: PaperDataContext,
        batch_id: str,
    ) -> tuple[PaperState, ...]:
        trade_date = steps[0].state.last_trade_date
        self._assert_not_committed(accounts, trade_date)
        return tuple(
            self._append_step(account, step, data_context, batch_id)
            for account, step in zip(accounts, steps, strict=True)
        )

    def _append_step(
        self,
        account: PaperAccount,
        step: EngineStep,
        data_context: PaperDataContext,
        batch_id: str,
    ) -> PaperState:
        connection = self._require_connection()
        trade_date = step.state.last_trade_date
        run_id = _run_id(batch_id, account.account_id, trade_date)
        previous_hash = self._previous_event_hash(account)
        event_hash = previous_hash

        for sequence_no, event in enumerate(step.events, start=1):
            body = _event_body(account.account_id, sequence_no, event)
            event_hash = _event_hash(previous_hash, body)
            event_id = hashlib.sha256(
                f"{run_id}:{sequence_no}:{event_hash}".encode("ascii")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO paper_events (
                    event_id, run_id, account_id, trade_date, sequence_no, event_type,
                    signal_date, order_id, action, quantity, reference_price,
                    execution_price, notional, commission, tax, slippage_cost,
                    cash_after, shares_after, close_price, equity_after, drawdown_after,
                    reason_code, payload_json, previous_event_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    event_id,
                    run_id,
                    account.account_id,
                    event.trade_date,
                    sequence_no,
                    event.event_type,
                    event.signal_date,
                    event.order_id,
                    event.action,
                    event.quantity,
                    event.reference_price,
                    event.execution_price,
                    event.notional,
                    event.commission,
                    event.tax,
                    event.slippage_cost,
                    event.cash_after,
                    event.shares_after,
                    event.close_price,
                    event.equity_after,
                    event.drawdown_after,
                    event.reason_code,
                    _canonical_json(event.payload),
                    previous_hash,
                    event_hash,
                ],
            )
            previous_hash = event_hash

        persisted_state = replace(step.state, last_event_hash=event_hash)
        now = datetime.now(UTC).replace(tzinfo=None)
        state_hash = _state_hash(persisted_state)
        connection.execute(
            """
            INSERT INTO paper_state (
                account_id, last_trade_date, cash, shares, last_close, equity,
                equity_peak, drawdown, last_target_position, pending_order_json,
                last_event_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET
                last_trade_date = excluded.last_trade_date,
                cash = excluded.cash,
                shares = excluded.shares,
                last_close = excluded.last_close,
                equity = excluded.equity,
                equity_peak = excluded.equity_peak,
                drawdown = excluded.drawdown,
                last_target_position = excluded.last_target_position,
                pending_order_json = excluded.pending_order_json,
                last_event_hash = excluded.last_event_hash,
                updated_at = excluded.updated_at
            """,
            _state_values(persisted_state, now),
        )
        connection.execute(
            """
            INSERT INTO paper_runs (
                run_id, batch_id, account_id, trade_date, status, started_at,
                completed_at, dataset_id, curated_sha256, manifest_generated_at,
                data_health_json, event_count, state_hash
            ) VALUES (?, ?, ?, ?, 'committed', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                batch_id,
                account.account_id,
                trade_date,
                now,
                now,
                data_context.dataset_id,
                data_context.curated_sha256,
                data_context.manifest_generated_at,
                _canonical_json(_data_context_payload(data_context)),
                len(step.events),
                state_hash,
            ],
        )
        return persisted_state

    def _assert_existing_accounts_match(self, accounts: Sequence[PaperAccount]) -> None:
        connection = self._require_connection()
        for account in accounts:
            result = connection.execute(
                "SELECT config_hash FROM paper_accounts WHERE account_id = ?",
                [account.account_id],
            ).fetchone()
            if result is None:
                raise PaperStoreError(f"模拟账户 {account.account_id} 不存在")
            if str(result[0]) != account.config_hash:
                raise PaperStoreError(f"模拟账户 {account.account_id} 的锁定配置不匹配")

    def _assert_not_committed(
        self,
        accounts: Sequence[PaperAccount],
        trade_date: date,
    ) -> None:
        connection = self._require_connection()
        for account in accounts:
            result = connection.execute(
                "SELECT 1 FROM paper_runs WHERE account_id = ? AND trade_date = ?",
                [account.account_id, trade_date],
            ).fetchone()
            if result is not None:
                raise PaperIdempotencyError(
                    f"模拟账户 {account.account_id} 在 {trade_date.isoformat()} 已提交"
                )

    def _previous_event_hash(self, account: PaperAccount) -> str:
        result = self._require_connection().execute(
            """
            SELECT event_hash FROM paper_events
            WHERE account_id = ?
            ORDER BY trade_date DESC, sequence_no DESC
            LIMIT 1
            """,
            [account.account_id],
        ).fetchone()
        return account.config_hash if result is None else str(result[0])

    def load_states(self, portfolio_id: str) -> tuple[PaperState, ...]:
        """Load the derived cache for a portfolio in its fixed strategy order."""
        rows = self._require_connection().execute(
            """
            SELECT
                state.account_id, state.last_trade_date, state.cash, state.shares,
                state.last_close, state.equity, state.equity_peak, state.drawdown,
                state.last_target_position, state.pending_order_json, state.last_event_hash
            FROM paper_state AS state
            INNER JOIN paper_accounts AS account USING (account_id)
            WHERE account.portfolio_id = ?
            ORDER BY
                CASE account.strategy
                    WHEN 'sma' THEN 1
                    WHEN 'momentum' THEN 2
                    ELSE 3
                END,
                state.account_id
            """,
            [portfolio_id],
        ).fetchall()
        return tuple(_state_from_row(row) for row in rows)

    def load_accounts(self, portfolio_id: str) -> tuple[PaperAccount, ...]:
        """Restore the immutable account definitions after their audit succeeds."""
        rows = self._require_connection().execute(
            """
            SELECT
                account_id, portfolio_id, symbol, instrument_kind, strategy,
                strategy_parameters_json, initial_cash, lot_size, commission_bps,
                minimum_commission, sell_tax_bps, slippage_bps, config_hash,
                created_market_date, engine_version
            FROM paper_accounts
            WHERE portfolio_id = ?
            ORDER BY
                CASE strategy
                    WHEN 'sma' THEN 1
                    WHEN 'momentum' THEN 2
                    ELSE 3
                END,
                account_id
            """,
            [portfolio_id],
        ).fetchall()
        return tuple(_account_from_row(row) for row in rows)

    def rebuild_state(self, account_id: str) -> PaperState:
        """Reconstruct one account solely from its immutable event history."""
        state, _states_by_run, _event_counts = self._rebuild_account(account_id)
        return state

    def audit_portfolio(self, portfolio_id: str) -> tuple[PaperState, ...]:
        """Verify all event chains, run summaries, and cached account states."""
        connection = self._require_connection()
        account_rows = connection.execute(
            """
            SELECT account_id FROM paper_accounts
            WHERE portfolio_id = ?
            ORDER BY
                CASE strategy
                    WHEN 'sma' THEN 1
                    WHEN 'momentum' THEN 2
                    ELSE 3
                END,
                account_id
            """,
            [portfolio_id],
        ).fetchall()
        if not account_rows:
            raise PaperAuditError(f"账户组 {portfolio_id} 不存在或没有账户")

        cached_by_account = {state.account_id: state for state in self.load_states(portfolio_id)}
        rebuilt_states: list[PaperState] = []
        for (account_id_raw,) in account_rows:
            account_id = str(account_id_raw)
            rebuilt, states_by_run, event_counts = self._rebuild_account(account_id)
            cached = cached_by_account.get(account_id)
            if cached is None:
                raise PaperAuditError(f"模拟账户 {account_id} 缺少状态缓存")
            if rebuilt != cached:
                raise PaperAuditError(f"模拟账户 {account_id} 的事件重建状态与缓存不一致")

            runs = connection.execute(
                """
                SELECT run_id, trade_date, event_count, state_hash, data_health_json
                FROM paper_runs
                WHERE account_id = ?
                ORDER BY trade_date, run_id
                """,
                [account_id],
            ).fetchall()
            if not runs:
                raise PaperAuditError(f"模拟账户 {account_id} 缺少已提交运行记录")
            for run_id_raw, trade_date, event_count, state_hash, data_health_json in runs:
                run_id = str(run_id_raw)
                rebuilt_at_run = states_by_run.get(run_id)
                if rebuilt_at_run is None:
                    raise PaperAuditError(f"运行 {run_id} 没有对应事件")
                if _as_date(trade_date) != rebuilt_at_run.last_trade_date:
                    raise PaperAuditError(f"运行 {run_id} 的日期与事件不一致")
                if int(event_count) != event_counts[run_id]:
                    raise PaperAuditError(f"运行 {run_id} 的事件数量不一致")
                if str(state_hash) != _state_hash(rebuilt_at_run):
                    raise PaperAuditError(f"运行 {run_id} 的状态哈希不一致")
                _assert_canonical_json(str(data_health_json), "运行数据健康上下文")
            rebuilt_states.append(rebuilt)
        return tuple(rebuilt_states)

    def portfolio_snapshot(self, portfolio_id: str) -> PaperPortfolioSnapshot:
        """Return an audited, JSON-ready view for reports and CLI status."""
        states = self.audit_portfolio(portfolio_id)
        state_by_account = {state.account_id: state for state in states}
        account_rows = self._require_connection().execute(
            """
            SELECT
                account_id, symbol, instrument_kind, strategy,
                strategy_parameters_json, initial_cash, lot_size, commission_bps,
                minimum_commission, sell_tax_bps, slippage_bps, config_hash,
                created_market_date, engine_version
            FROM paper_accounts
            WHERE portfolio_id = ?
            ORDER BY
                CASE strategy
                    WHEN 'sma' THEN 1
                    WHEN 'momentum' THEN 2
                    ELSE 3
                END,
                account_id
            """,
            [portfolio_id],
        ).fetchall()
        accounts: list[PaperAccountSnapshot] = []
        for row in account_rows:
            (
                account_id_raw,
                symbol,
                instrument_kind,
                strategy,
                strategy_parameters_json,
                initial_cash,
                lot_size,
                commission_bps,
                minimum_commission,
                sell_tax_bps,
                slippage_bps,
                config_hash,
                created_market_date,
                engine_version,
            ) = row
            account_id = str(account_id_raw)
            state = state_by_account[account_id]
            latest_run = self._latest_run_snapshot(account_id)
            accounts.append(
                {
                    "account_id": account_id,
                    "symbol": str(symbol),
                    "instrument_kind": str(instrument_kind),
                    "strategy": str(strategy),
                    "strategy_parameters": _load_canonical_object(
                        str(strategy_parameters_json), "策略参数"
                    ),
                    "ledger_config": {
                        "initial_cash": float(initial_cash),
                        "lot_size": int(lot_size),
                        "commission_bps": float(commission_bps),
                        "minimum_commission": float(minimum_commission),
                        "sell_tax_bps": float(sell_tax_bps),
                        "slippage_bps": float(slippage_bps),
                    },
                    "config_hash": str(config_hash),
                    "created_market_date": _as_date(created_market_date).isoformat(),
                    "engine_version": str(engine_version),
                    "state": _state_payload(state),
                    "latest_run": latest_run,
                }
            )
        return {"portfolio_id": portfolio_id, "accounts": accounts}

    def _latest_run_snapshot(self, account_id: str) -> dict[str, object]:
        row = self._require_connection().execute(
            """
            SELECT
                run_id, batch_id, trade_date, dataset_id, curated_sha256,
                manifest_generated_at, data_health_json, event_count, state_hash
            FROM paper_runs
            WHERE account_id = ?
            ORDER BY trade_date DESC, run_id DESC
            LIMIT 1
            """,
            [account_id],
        ).fetchone()
        if row is None:
            raise PaperAuditError(f"模拟账户 {account_id} 缺少最新运行记录")
        (
            run_id,
            batch_id,
            trade_date,
            dataset_id,
            curated_sha256,
            manifest_generated_at,
            data_health_json,
            event_count,
            state_hash,
        ) = row
        return {
            "run_id": str(run_id),
            "batch_id": str(batch_id),
            "trade_date": _as_date(trade_date).isoformat(),
            "dataset_id": str(dataset_id),
            "curated_sha256": str(curated_sha256),
            "manifest_generated_at": str(manifest_generated_at),
            "data_health": _load_canonical_object(
                str(data_health_json), "运行数据健康上下文"
            ),
            "event_count": int(event_count),
            "state_hash": str(state_hash),
        }

    def _rebuild_account(
        self,
        account_id: str,
    ) -> tuple[PaperState, dict[str, PaperState], dict[str, int]]:
        connection = self._require_connection()
        account_row = connection.execute(
            """
            SELECT
                config_hash, symbol, instrument_kind, strategy,
                strategy_parameters_json, initial_cash, lot_size, commission_bps,
                minimum_commission, sell_tax_bps, slippage_bps, engine_version
            FROM paper_accounts
            WHERE account_id = ?
            """,
            [account_id],
        ).fetchone()
        if account_row is None:
            raise PaperAuditError(f"模拟账户 {account_id} 不存在")
        config_hash, strategy_name = _validate_stored_account_configuration(account_row)
        rows = connection.execute(
            """
            SELECT
                run_id, trade_date, sequence_no, event_type, signal_date, order_id,
                action, quantity, reference_price, execution_price, notional,
                commission, tax, slippage_cost, cash_after, shares_after,
                close_price, equity_after, drawdown_after, reason_code, payload_json,
                previous_event_hash, event_hash
            FROM paper_events
            WHERE account_id = ?
            ORDER BY trade_date, run_id, sequence_no
            """,
            [account_id],
        ).fetchall()
        if not rows:
            raise PaperAuditError(f"模拟账户 {account_id} 缺少事件")

        previous_hash = config_hash
        expected_sequence = 1
        current_run_id: str | None = None
        run_state: PaperState | None = None
        states_by_run: dict[str, PaperState] = {}
        event_counts: dict[str, int] = {}
        cash = 0.0
        shares = 0
        close = 0.0
        equity = 0.0
        equity_peak = 0.0
        drawdown = 0.0
        last_trade_date: date | None = None
        last_target_position: int | None = None
        pending_order: PendingOrder | None = None
        first_event = True

        for row in rows:
            (
                run_id_raw,
                trade_date_raw,
                sequence_no,
                event_type,
                signal_date_raw,
                order_id_raw,
                action_raw,
                quantity,
                reference_price,
                execution_price,
                notional,
                commission,
                tax,
                slippage_cost,
                cash_after,
                shares_after,
                close_price,
                equity_after,
                drawdown_after,
                reason_code,
                payload_json,
                stored_previous_hash,
                stored_event_hash,
            ) = row
            run_id = str(run_id_raw)
            if current_run_id is not None and run_id != current_run_id:
                if run_state is None:
                    raise PaperAuditError(f"运行 {current_run_id} 没有可重建状态")
                states_by_run[current_run_id] = run_state
                expected_sequence = 1
            current_run_id = run_id
            if int(sequence_no) != expected_sequence:
                raise PaperAuditError(f"运行 {run_id} 的事件序号不连续")
            expected_sequence += 1
            event_counts[run_id] = event_counts.get(run_id, 0) + 1

            payload = _load_canonical_object(str(payload_json), "事件 payload")
            if first_event:
                _assert_account_created_payload(
                    account_id,
                    config_hash,
                    strategy_name,
                    str(event_type),
                    payload,
                )
                first_event = False
            body = _stored_event_body(
                account_id,
                int(sequence_no),
                _as_date(trade_date_raw),
                str(event_type),
                _as_date(signal_date_raw) if signal_date_raw is not None else None,
                str(order_id_raw) if order_id_raw is not None else None,
                str(action_raw) if action_raw is not None else None,
                int(quantity),
                float(reference_price) if reference_price is not None else None,
                float(execution_price) if execution_price is not None else None,
                float(notional),
                float(commission),
                float(tax),
                float(slippage_cost),
                float(cash_after),
                int(shares_after),
                float(close_price),
                float(equity_after),
                float(drawdown_after),
                str(reason_code) if reason_code is not None else None,
                payload,
            )
            if str(stored_previous_hash) != previous_hash or str(stored_event_hash) != _event_hash(
                previous_hash, body
            ):
                raise PaperAuditError(f"模拟账户 {account_id} 的事件哈希链不一致")
            previous_hash = str(stored_event_hash)

            cash = float(cash_after)
            shares = int(shares_after)
            close = float(close_price)
            equity = float(equity_after)
            drawdown = float(drawdown_after)
            last_trade_date = _as_date(trade_date_raw)
            _assert_state_numbers(account_id, cash, shares, close, equity, drawdown)
            equity_peak = max(equity_peak, equity)
            expected_equity = cash + shares * close
            expected_drawdown = equity / equity_peak - 1.0
            if not math.isclose(equity, expected_equity, rel_tol=0.0, abs_tol=1e-8):
                raise PaperAuditError(f"模拟账户 {account_id} 的权益对账失败")
            if not math.isclose(drawdown, expected_drawdown, rel_tol=0.0, abs_tol=1e-10):
                raise PaperAuditError(f"模拟账户 {account_id} 的回撤对账失败")

            if str(event_type) == "SIGNAL_GENERATED":
                target = payload.get("target_position")
                if target is not None:
                    if type(target) is not int or target not in {0, 1}:
                        raise PaperAuditError(f"模拟账户 {account_id} 的信号目标无效")
                    last_target_position = target
            elif str(event_type) == "ORDER_CREATED":
                if order_id_raw is None or signal_date_raw is None or action_raw is None:
                    raise PaperAuditError(f"模拟账户 {account_id} 的待成交订单字段不完整")
                pending_order = PendingOrder(
                    order_id=str(order_id_raw),
                    signal_date=_as_date(signal_date_raw),
                    action=_as_action(action_raw, "订单事件"),
                )
            elif str(event_type) in {"ORDER_FILLED", "ORDER_SKIPPED"}:
                if pending_order is None or str(order_id_raw) != pending_order.order_id:
                    raise PaperAuditError(f"模拟账户 {account_id} 的订单事件无法匹配待成交订单")
                pending_order = None

            run_state = PaperState(
                account_id=account_id,
                last_trade_date=last_trade_date,
                cash=cash,
                shares=shares,
                last_close=close,
                equity=equity,
                equity_peak=equity_peak,
                drawdown=drawdown,
                last_target_position=last_target_position,
                pending_order=pending_order,
                last_event_hash=previous_hash,
            )

        if current_run_id is None or run_state is None:
            raise PaperAuditError(f"模拟账户 {account_id} 缺少可重建状态")
        states_by_run[current_run_id] = run_state
        return run_state, states_by_run, event_counts

    @staticmethod
    def _validate_batch(accounts: Sequence[PaperAccount], steps: Sequence[EngineStep]) -> None:
        if not accounts or len(accounts) != len(steps):
            raise PaperStoreError("账户与状态步骤必须一一对应且不能为空")
        trade_dates: set[date] = set()
        for account, step in zip(accounts, steps, strict=True):
            if step.state.account_id != account.account_id:
                raise PaperStoreError("状态步骤与模拟账户不匹配")
            if not step.events:
                raise PaperStoreError("每个已提交步骤至少需要一条事件")
            if any(event.trade_date != step.state.last_trade_date for event in step.events):
                raise PaperStoreError("同一运行步骤的事件日期必须一致")
            trade_dates.add(step.state.last_trade_date)
        if len(trade_dates) != 1:
            raise PaperStoreError("批次中的所有账户必须处理同一个行情日期")

    def _require_connection(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RuntimeError("账本存储尚未打开")
        return self._connection


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _data_context_payload(context: PaperDataContext) -> dict[str, object]:
    return {
        "dataset_id": context.dataset_id,
        "curated_sha256": context.curated_sha256,
        "manifest_generated_at": context.manifest_generated_at,
        "as_of_date": context.as_of_date.isoformat(),
        "data_start_date": context.data_start_date.isoformat(),
        "data_end_date": context.data_end_date.isoformat(),
        "sources": context.sources,
        "warning_codes": list(context.warning_codes),
        "upstream_errors": context.upstream_errors,
        "business_days_stale": context.business_days_stale,
        "update_summary_end": context.update_summary_end,
    }


def _event_body(
    account_id: str,
    sequence_no: int,
    event: PaperEventDraft,
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "trade_date": event.trade_date.isoformat(),
        "sequence_no": sequence_no,
        "event_type": event.event_type,
        "signal_date": event.signal_date.isoformat() if event.signal_date else None,
        "order_id": event.order_id,
        "action": event.action,
        "quantity": event.quantity,
        "reference_price": event.reference_price,
        "execution_price": event.execution_price,
        "notional": event.notional,
        "commission": event.commission,
        "tax": event.tax,
        "slippage_cost": event.slippage_cost,
        "cash_after": event.cash_after,
        "shares_after": event.shares_after,
        "close_price": event.close_price,
        "equity_after": event.equity_after,
        "drawdown_after": event.drawdown_after,
        "reason_code": event.reason_code,
        "payload": event.payload,
    }


def _event_hash(previous_event_hash: str, body: dict[str, object]) -> str:
    canonical = _canonical_json(
        {"previous_event_hash": previous_event_hash, "event": body}
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_id(batch_id: str, account_id: str, trade_date: date) -> str:
    identity = _canonical_json(
        {"batch_id": batch_id, "account_id": account_id, "trade_date": trade_date.isoformat()}
    )
    return "paper-run-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _pending_order_payload(state: PaperState) -> dict[str, object] | None:
    if state.pending_order is None:
        return None
    return {
        "order_id": state.pending_order.order_id,
        "signal_date": state.pending_order.signal_date.isoformat(),
        "action": state.pending_order.action,
    }


def _state_payload(state: PaperState) -> dict[str, object]:
    return {
        "account_id": state.account_id,
        "last_trade_date": state.last_trade_date.isoformat(),
        "cash": state.cash,
        "shares": state.shares,
        "last_close": state.last_close,
        "equity": state.equity,
        "equity_peak": state.equity_peak,
        "drawdown": state.drawdown,
        "last_target_position": state.last_target_position,
        "pending_order": _pending_order_payload(state),
        "last_event_hash": state.last_event_hash,
    }


def _state_hash(state: PaperState) -> str:
    return hashlib.sha256(_canonical_json(_state_payload(state)).encode("utf-8")).hexdigest()


def _state_values(state: PaperState, updated_at: datetime) -> list[object]:
    pending_order = _pending_order_payload(state)
    return [
        state.account_id,
        state.last_trade_date,
        state.cash,
        state.shares,
        state.last_close,
        state.equity,
        state.equity_peak,
        state.drawdown,
        state.last_target_position,
        _canonical_json(pending_order) if pending_order is not None else None,
        state.last_event_hash,
        updated_at,
    ]


def _state_from_row(row: tuple[object, ...]) -> PaperState:
    (
        account_id,
        last_trade_date,
        cash,
        shares,
        last_close,
        equity,
        equity_peak,
        drawdown,
        last_target_position,
        pending_order_json,
        last_event_hash,
    ) = row
    pending_payload = (
        _load_canonical_object(str(pending_order_json), "待成交订单")
        if pending_order_json is not None
        else None
    )
    pending_order = (
        PendingOrder(
            order_id=_required_string(pending_payload, "order_id", "待成交订单"),
            signal_date=_as_date(pending_payload["signal_date"]),
            action=_required_action(pending_payload, "待成交订单"),
        )
        if pending_payload is not None
        else None
    )
    target_position = (
        _as_int(last_target_position, "状态缓存目标仓位")
        if last_target_position is not None
        else None
    )
    if target_position not in {None, 0, 1}:
        raise PaperAuditError("状态缓存中的目标仓位无效")
    return PaperState(
        account_id=str(account_id),
        last_trade_date=_as_date(last_trade_date),
        cash=_as_float(cash, "状态缓存现金"),
        shares=_as_int(shares, "状态缓存份额"),
        last_close=_as_float(last_close, "状态缓存收盘价"),
        equity=_as_float(equity, "状态缓存权益"),
        equity_peak=_as_float(equity_peak, "状态缓存权益峰值"),
        drawdown=_as_float(drawdown, "状态缓存回撤"),
        last_target_position=target_position,
        pending_order=pending_order,
        last_event_hash=str(last_event_hash),
    )


def _account_from_row(row: tuple[object, ...]) -> PaperAccount:
    (
        account_id,
        portfolio_id,
        symbol,
        instrument_kind,
        strategy_name,
        strategy_parameters_json,
        initial_cash,
        lot_size,
        commission_bps,
        minimum_commission,
        sell_tax_bps,
        slippage_bps,
        config_hash,
        created_market_date,
        engine_version,
    ) = row
    strategy = _required_string({"strategy": strategy_name}, "strategy", "账户配置")
    if strategy not in {"sma", "momentum"}:
        raise PaperAuditError("账户配置中的策略无效")
    parameters = _load_canonical_object(str(strategy_parameters_json), "账户策略参数")
    if parameters.get("name") != strategy:
        raise PaperAuditError("账户策略参数与策略名称不一致")
    strategy_spec = StrategySpec(
        name=cast(PaperStrategyName, strategy),
        short_window=_optional_int(parameters.get("short_window"), "短期均线窗口"),
        long_window=_optional_int(parameters.get("long_window"), "长期均线窗口"),
        momentum_lookback=_optional_int(
            parameters.get("momentum_lookback"), "动量回看窗口"
        ),
    )
    ledger_config = LedgerConfig(
        initial_cash=_as_float(initial_cash, "账户初始现金"),
        lot_size=_as_int(lot_size, "账户每手数量"),
        commission_bps=_as_float(commission_bps, "账户佣金"),
        minimum_commission=_as_float(minimum_commission, "账户最低佣金"),
        sell_tax_bps=_as_float(sell_tax_bps, "账户卖出税费"),
        slippage_bps=_as_float(slippage_bps, "账户滑点"),
    )
    validate_ledger_config(ledger_config)
    return PaperAccount(
        account_id=_required_string({"account_id": account_id}, "account_id", "账户配置"),
        portfolio_id=_required_string({"portfolio_id": portfolio_id}, "portfolio_id", "账户配置"),
        symbol=_required_string({"symbol": symbol}, "symbol", "账户配置"),
        instrument_kind=_required_string(
            {"instrument_kind": instrument_kind}, "instrument_kind", "账户配置"
        ),
        strategy=strategy_spec,
        ledger_config=ledger_config,
        created_market_date=_as_date(created_market_date),
        engine_version=_required_string(
            {"engine_version": engine_version}, "engine_version", "账户配置"
        ),
        config_hash=_required_string({"config_hash": config_hash}, "config_hash", "账户配置"),
    )


def _stored_event_body(
    account_id: str,
    sequence_no: int,
    trade_date: date,
    event_type: str,
    signal_date: date | None,
    order_id: str | None,
    action: str | None,
    quantity: int,
    reference_price: float | None,
    execution_price: float | None,
    notional: float,
    commission: float,
    tax: float,
    slippage_cost: float,
    cash_after: float,
    shares_after: int,
    close_price: float,
    equity_after: float,
    drawdown_after: float,
    reason_code: str | None,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "trade_date": trade_date.isoformat(),
        "sequence_no": sequence_no,
        "event_type": event_type,
        "signal_date": signal_date.isoformat() if signal_date else None,
        "order_id": order_id,
        "action": action,
        "quantity": quantity,
        "reference_price": reference_price,
        "execution_price": execution_price,
        "notional": notional,
        "commission": commission,
        "tax": tax,
        "slippage_cost": slippage_cost,
        "cash_after": cash_after,
        "shares_after": shares_after,
        "close_price": close_price,
        "equity_after": equity_after,
        "drawdown_after": drawdown_after,
        "reason_code": reason_code,
        "payload": payload,
    }


def _load_canonical_object(serialized: str, label: str) -> dict[str, object]:
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise PaperAuditError(f"{label} 不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise PaperAuditError(f"{label} 必须是 JSON 对象")
    if _canonical_json(payload) != serialized:
        raise PaperAuditError(f"{label} 不是规范化 JSON")
    return payload


def _assert_canonical_json(serialized: str, label: str) -> None:
    _load_canonical_object(serialized, label)


def _required_string(payload: dict[str, object], field: str, label: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise PaperAuditError(f"{label} 缺少 {field}")
    return value


def _required_action(payload: dict[str, object], label: str) -> PaperAction:
    value = _required_string(payload, "action", label)
    return _as_action(value, label)


def _as_action(value: object, label: str) -> PaperAction:
    if value not in {"BUY", "SELL"}:
        raise PaperAuditError(f"{label} 的 action 无效")
    return cast(PaperAction, value)


def _as_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperAuditError(f"{label} 无效")
    return float(value)


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperAuditError(f"{label} 无效")
    return value


def _optional_int(value: object, label: str) -> int | None:
    return None if value is None else _as_int(value, label)


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise PaperAuditError("账本日期无效") from exc
    raise PaperAuditError("账本日期无效")


def _assert_state_numbers(
    account_id: str,
    cash: float,
    shares: int,
    close: float,
    equity: float,
    drawdown: float,
) -> None:
    numeric_values = (cash, close, equity, drawdown)
    if not all(math.isfinite(value) for value in numeric_values):
        raise PaperAuditError(f"模拟账户 {account_id} 的状态包含非有限数字")
    if cash < 0.0 or shares < 0 or close <= 0.0 or equity <= 0.0:
        raise PaperAuditError(f"模拟账户 {account_id} 的状态数值无效")


def _validate_stored_account_configuration(
    row: tuple[object, ...],
) -> tuple[str, str]:
    (
        config_hash,
        symbol,
        instrument_kind,
        strategy,
        strategy_parameters_json,
        initial_cash,
        lot_size,
        commission_bps,
        minimum_commission,
        sell_tax_bps,
        slippage_bps,
        engine_version,
    ) = row
    strategy_name = _required_string({"strategy": strategy}, "strategy", "账户锁定配置")
    strategy_parameters = _load_canonical_object(
        str(strategy_parameters_json), "账户锁定策略参数"
    )
    if strategy_parameters.get("name") != strategy_name:
        raise PaperAuditError("账户锁定配置中的策略字段不一致")
    expected_hash = hashlib.sha256(
        json.dumps(
            {
                "symbol": _required_string({"symbol": symbol}, "symbol", "账户锁定配置"),
                "instrument_kind": _required_string(
                    {"instrument_kind": instrument_kind}, "instrument_kind", "账户锁定配置"
                ),
                "strategy": strategy_parameters,
                "ledger_config": {
                    "initial_cash": _as_float(initial_cash, "账户锁定初始现金"),
                    "lot_size": _as_int(lot_size, "账户锁定每手数量"),
                    "commission_bps": _as_float(commission_bps, "账户锁定佣金"),
                    "minimum_commission": _as_float(
                        minimum_commission, "账户锁定最低佣金"
                    ),
                    "sell_tax_bps": _as_float(sell_tax_bps, "账户锁定卖出税费"),
                    "slippage_bps": _as_float(slippage_bps, "账户锁定滑点"),
                },
                "engine_version": _required_string(
                    {"engine_version": engine_version}, "engine_version", "账户锁定配置"
                ),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    if str(config_hash) != expected_hash:
        raise PaperAuditError("账户锁定配置哈希不一致")
    return str(config_hash), strategy_name


def _assert_account_created_payload(
    account_id: str,
    config_hash: str,
    strategy_name: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    if event_type != "ACCOUNT_CREATED":
        raise PaperAuditError("账户首条事件必须是 ACCOUNT_CREATED")
    if payload.get("account_id") != account_id:
        raise PaperAuditError("ACCOUNT_CREATED 的账户标识与锁定配置不一致")
    if payload.get("config_hash") != config_hash:
        raise PaperAuditError("ACCOUNT_CREATED 的配置哈希与锁定配置不一致")
    if payload.get("strategy") != strategy_name:
        raise PaperAuditError("ACCOUNT_CREATED 的策略与锁定配置不一致")
