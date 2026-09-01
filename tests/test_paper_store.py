from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from finance_lab.cash_distributions import CashDistribution
from finance_lab.ledger import LedgerConfig
from finance_lab.paper_engine import advance_one_bar, initialize_account
from finance_lab.paper_models import (
    EngineStep,
    PaperAccount,
    PaperDataContext,
    PaperState,
    make_default_accounts,
)
from finance_lab.paper_store import PaperAuditError, PaperStore
from finance_lab.share_adjustments import ShareAdjustment


def test_schema_is_versioned_and_leaves_daily_prices_untouched(tmp_path: Path) -> None:
    database = tmp_path / "finance_lab.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE daily_prices AS SELECT 1 AS sentinel")

    with PaperStore(database) as store:
        store.ensure_schema()

    with duckdb.connect(str(database), read_only=True) as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
            ).fetchall()
        }
        version = connection.execute(
            "SELECT MAX(schema_version) FROM paper_schema_metadata"
        ).fetchone()
        state_columns = {
            str(row[0])
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'paper_state'"
            ).fetchall()
        }
        daily_prices = connection.execute("SELECT sentinel FROM daily_prices").fetchall()

    assert table_names == {
        "daily_prices",
        "paper_schema_metadata",
        "paper_accounts",
        "paper_runs",
        "paper_events",
        "paper_state",
    }
    assert version == (2,)
    assert "distribution_entitlements_json" in state_columns
    assert daily_prices == [(1,)]


def test_schema_migrates_v1_state_without_losing_cached_values(tmp_path: Path) -> None:
    database = tmp_path / "finance_lab.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "CREATE TABLE paper_schema_metadata ("
            "schema_version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute("INSERT INTO paper_schema_metadata VALUES (1, CURRENT_TIMESTAMP)")
        connection.execute(
            """
            CREATE TABLE paper_state (
                account_id VARCHAR PRIMARY KEY,
                last_trade_date DATE NOT NULL,
                cash DOUBLE NOT NULL,
                shares BIGINT NOT NULL,
                last_close DOUBLE NOT NULL,
                equity DOUBLE NOT NULL,
                equity_peak DOUBLE NOT NULL,
                drawdown DOUBLE NOT NULL,
                last_target_position INTEGER,
                pending_order_json VARCHAR,
                last_event_hash VARCHAR NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO paper_state VALUES (
                'legacy-account', DATE '2026-01-02', 1000.0, 0, 10.0,
                1000.0, 1000.0, 0.0, NULL, NULL, 'legacy-hash', CURRENT_TIMESTAMP
            )
            """
        )

    with PaperStore(database) as store:
        store.ensure_schema()

    with duckdb.connect(str(database), read_only=True) as connection:
        migrated = connection.execute(
            "SELECT cash, last_event_hash, distribution_entitlements_json "
            "FROM paper_state WHERE account_id = 'legacy-account'"
        ).fetchone()
        versions = connection.execute(
            "SELECT schema_version FROM paper_schema_metadata ORDER BY schema_version"
        ).fetchall()

    assert migrated == (1000.0, "legacy-hash", "[]")
    assert versions == [(1,), (2,)]


def _history(periods: int) -> pd.DataFrame:
    prices = [float(value) for value in range(100, 100 + periods)]
    return pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2026-01-02", periods=periods),
            "open": prices,
            "close": prices,
        }
    )


def _data_context(as_of_date: pd.Timestamp) -> PaperDataContext:
    return PaperDataContext(
        dataset_id="d" * 64,
        curated_sha256="c" * 64,
        manifest_generated_at="2026-08-31T00:00:00Z",
        as_of_date=as_of_date.date(),
        data_start_date=pd.Timestamp("2026-01-02").date(),
        data_end_date=as_of_date.date(),
        sources={"akshare": 1},
        warning_codes=(),
        upstream_errors={},
        business_days_stale=0,
        update_summary_end=as_of_date.date().isoformat(),
    )


def _accounts_and_initial_steps() -> tuple[
    PaperAccount,
    PaperAccount,
    EngineStep,
    EngineStep,
    PaperDataContext,
]:
    history = _history(121)
    accounts = make_default_accounts(
        "default",
        history.iloc[-1]["trade_date"].date(),
        LedgerConfig(commission_bps=0.0, minimum_commission=0.0, slippage_bps=0.0),
    )
    steps = tuple(initialize_account(account, history) for account in accounts)
    return (
        accounts[0],
        accounts[1],
        steps[0],
        steps[1],
        _data_context(history.iloc[-1]["trade_date"]),
    )


def test_batch_failure_rolls_back_both_accounts_for_the_trade_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, initial_context = (
        _accounts_and_initial_steps()
    )
    next_history = _history(122)
    first_next = advance_one_bar(first_account, first_step.state, next_history)
    second_next = advance_one_bar(second_account, second_step.state, next_history)
    next_context = _data_context(next_history.iloc[-1]["trade_date"])

    with PaperStore(database) as store:
        store.ensure_schema()
        store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            initial_context,
            batch_id="initial",
        )
        original_append = store._append_step
        append_calls = 0

        def fail_second_append(
            account: PaperAccount,
            step: EngineStep,
            data_context: PaperDataContext,
            batch_id: str,
        ) -> PaperState:
            nonlocal append_calls
            append_calls += 1
            if append_calls == 2:
                raise RuntimeError("injected write failure")
            return original_append(account, step, data_context, batch_id)

        monkeypatch.setattr(store, "_append_step", fail_second_append)
        with pytest.raises(RuntimeError, match="injected write failure"):
            store.commit_batch(
                (first_account, second_account),
                (first_next, second_next),
                next_context,
                batch_id="next-day",
            )

        next_date = next_history.iloc[-1]["trade_date"].date()
        runs = store._require_connection().execute(
            "SELECT COUNT(*) FROM paper_runs WHERE trade_date = ?", [next_date]
        ).fetchone()
        assert runs == (0,)


def test_duplicate_account_date_is_rejected_without_new_rows(tmp_path: Path) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, context = _accounts_and_initial_steps()

    with PaperStore(database) as store:
        store.ensure_schema()
        store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            context,
            batch_id="initial",
        )
        before = store._require_connection().execute(
            "SELECT COUNT(*) FROM paper_events"
        ).fetchone()

        with pytest.raises(RuntimeError, match="已提交"):
            store.commit_batch(
                (first_account, second_account),
                (first_step, second_step),
                context,
                batch_id="retry",
            )

        after = store._require_connection().execute(
            "SELECT COUNT(*) FROM paper_events"
        ).fetchone()

    assert before == after


def test_rebuild_matches_cached_state_and_tampering_fails_audit(tmp_path: Path) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, initial_context = (
        _accounts_and_initial_steps()
    )
    next_history = _history(122)
    next_context = _data_context(next_history.iloc[-1]["trade_date"])

    with PaperStore(database) as store:
        store.ensure_schema()
        initial_states = store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            initial_context,
            batch_id="initial",
        )
        next_steps = (
            advance_one_bar(first_account, initial_states[0], next_history),
            advance_one_bar(second_account, initial_states[1], next_history),
        )
        persisted_states = store.commit_batch(
            (first_account, second_account),
            next_steps,
            next_context,
            batch_id="next-day",
        )

        assert store.rebuild_state(first_account.account_id) == persisted_states[0]
        assert store.load_states("default") == persisted_states
        assert store.audit_portfolio("default") == persisted_states

        store._require_connection().execute(
            """
            UPDATE paper_events
            SET cash_after = cash_after + 1
            WHERE event_id = (
                SELECT event_id FROM paper_events
                WHERE account_id = ?
                ORDER BY trade_date, sequence_no
                LIMIT 1
            )
            """,
            [first_account.account_id],
        )

        with pytest.raises(PaperAuditError, match="哈希链"):
            store.audit_portfolio("default")


def test_store_persists_and_rebuilds_cash_distribution_entitlements(
    tmp_path: Path,
) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, initial_context = (
        _accounts_and_initial_steps()
    )
    record_history = _history(122)
    payment_history = _history(123)
    record_date = record_history.iloc[-1]["trade_date"].date()
    payment_date = payment_history.iloc[-1]["trade_date"].date()
    distribution = CashDistribution(
        action_id="distribution-2026",
        symbol=first_account.symbol,
        record_date=record_date,
        ex_date=record_date,
        payment_date=payment_date,
        cash_per_share=0.5,
        currency="CNY",
        source_url="https://example.com/distributions/2026",
        source_published_at=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    with PaperStore(database) as store:
        store.ensure_schema()
        initial_states = store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            initial_context,
            batch_id="initial",
        )
        record_steps = (
            advance_one_bar(
                first_account,
                initial_states[0],
                record_history,
                cash_distributions=(distribution,),
            ),
            advance_one_bar(
                second_account,
                initial_states[1],
                record_history,
                cash_distributions=(distribution,),
            ),
        )
        record_states = store.commit_batch(
            (first_account, second_account),
            record_steps,
            _data_context(record_history.iloc[-1]["trade_date"]),
            batch_id="record-date",
        )

        cached_record_states = store.load_states("default")
        assert cached_record_states == record_states
        assert store.rebuild_state(first_account.account_id) == record_states[0]
        assert len(record_states[0].distribution_entitlements) == 1

        payment_steps = (
            advance_one_bar(
                first_account,
                record_states[0],
                payment_history,
                cash_distributions=(distribution,),
            ),
            advance_one_bar(
                second_account,
                record_states[1],
                payment_history,
                cash_distributions=(distribution,),
            ),
        )
        payment_states = store.commit_batch(
            (first_account, second_account),
            payment_steps,
            _data_context(payment_history.iloc[-1]["trade_date"]),
            batch_id="payment-date",
        )

        assert payment_states[0].distribution_entitlements == ()
        assert store.load_states("default") == payment_states
        assert store.audit_portfolio("default") == payment_states


def test_store_rebuilds_and_indexes_share_adjustment_events(tmp_path: Path) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, initial_context = (
        _accounts_and_initial_steps()
    )
    buy_history = _history(122)
    adjustment_history = _history(123)
    effective_date = adjustment_history.iloc[-1]["trade_date"].date()
    adjustment = ShareAdjustment(
        action_id="share-adjustment-store-test",
        symbol=first_account.symbol,
        effective_date=effective_date,
        ratio_numerator=3,
        ratio_denominator=8,
        source_url="https://example.com/share-adjustment/store-test",
        source_published_at=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    with PaperStore(database) as store:
        store.ensure_schema()
        initial_states = store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            initial_context,
            batch_id="initial",
        )
        buy_states = store.commit_batch(
            (first_account, second_account),
            (
                advance_one_bar(first_account, initial_states[0], buy_history),
                advance_one_bar(second_account, initial_states[1], buy_history),
            ),
            _data_context(buy_history.iloc[-1]["trade_date"]),
            batch_id="buy",
        )
        adjusted_states = store.commit_batch(
            (first_account, second_account),
            (
                advance_one_bar(
                    first_account,
                    buy_states[0],
                    adjustment_history,
                    share_adjustments=(adjustment,),
                ),
                advance_one_bar(
                    second_account,
                    buy_states[1],
                    adjustment_history,
                    share_adjustments=(adjustment,),
                ),
            ),
            _data_context(adjustment_history.iloc[-1]["trade_date"]),
            batch_id="share-adjustment",
        )

        assert [state.shares for state in adjusted_states] == [150, 150]
        assert store.audit_portfolio("default") == adjusted_states
        assert store.observed_share_adjustment_ids("default") == {
            first_account.account_id: frozenset({adjustment.action_id}),
            second_account.account_id: frozenset({adjustment.action_id}),
        }


def test_audit_rejects_self_consistent_but_false_share_adjustment_ratio(
    tmp_path: Path,
) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, initial_context = (
        _accounts_and_initial_steps()
    )
    buy_history = _history(122)
    adjustment_history = _history(123)
    adjustment = ShareAdjustment(
        action_id="false-ratio-test",
        symbol=first_account.symbol,
        effective_date=adjustment_history.iloc[-1]["trade_date"].date(),
        ratio_numerator=3,
        ratio_denominator=8,
        source_url="https://example.com/share-adjustment/false-ratio",
        source_published_at=datetime(2026, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    with PaperStore(database) as store:
        store.ensure_schema()
        initial_states = store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            initial_context,
            batch_id="initial",
        )
        buy_states = store.commit_batch(
            (first_account, second_account),
            (
                advance_one_bar(first_account, initial_states[0], buy_history),
                advance_one_bar(second_account, initial_states[1], buy_history),
            ),
            _data_context(buy_history.iloc[-1]["trade_date"]),
            batch_id="buy",
        )
        valid_steps = (
            advance_one_bar(
                first_account,
                buy_states[0],
                adjustment_history,
                share_adjustments=(adjustment,),
            ),
            advance_one_bar(
                second_account,
                buy_states[1],
                adjustment_history,
                share_adjustments=(adjustment,),
            ),
        )
        events = list(valid_steps[0].events)
        false_payload = {**events[0].payload, "ratio_numerator": 2, "ratio_denominator": 1}
        events[0] = replace(events[0], payload=false_payload)
        false_step = replace(valid_steps[0], events=tuple(events))
        store.commit_batch(
            (first_account, second_account),
            (false_step, valid_steps[1]),
            _data_context(adjustment_history.iloc[-1]["trade_date"]),
            batch_id="false-ratio",
        )

        with pytest.raises(PaperAuditError, match="份额调整"):
            store.audit_portfolio("default")


def test_read_only_store_audits_and_returns_snapshot_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, context = _accounts_and_initial_steps()

    with PaperStore(database) as store:
        store.ensure_schema()
        expected_states = store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            context,
            batch_id="initial",
        )

    before_mtime = database.stat().st_mtime_ns
    with PaperStore(database, read_only=True) as store:
        with pytest.raises(RuntimeError, match="只读"):
            store.ensure_schema()
        assert store.audit_portfolio("default") == expected_states
        snapshot = store.portfolio_snapshot("default")

    assert database.stat().st_mtime_ns == before_mtime
    assert snapshot["portfolio_id"] == "default"
    assert [item["account_id"] for item in snapshot["accounts"]] == [
        first_account.account_id,
        second_account.account_id,
    ]
    assert snapshot["accounts"][0]["state"]["cash"] == pytest.approx(
        expected_states[0].cash
    )


def test_audit_rejects_mutated_locked_account_configuration(tmp_path: Path) -> None:
    database = tmp_path / "finance_lab.duckdb"
    first_account, second_account, first_step, second_step, context = _accounts_and_initial_steps()

    with PaperStore(database) as store:
        store.ensure_schema()
        store.initialize_portfolio(
            (first_account, second_account),
            (first_step, second_step),
            context,
            batch_id="initial",
        )
        store._require_connection().execute(
            "UPDATE paper_accounts SET strategy = ? WHERE account_id = ?",
            ["momentum", first_account.account_id],
        )

        with pytest.raises(PaperAuditError, match="锁定配置"):
            store.audit_portfolio("default")
