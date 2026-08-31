from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

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
            "SELECT schema_version FROM paper_schema_metadata"
        ).fetchone()
        daily_prices = connection.execute("SELECT sentinel FROM daily_prices").fetchall()

    assert table_names == {
        "daily_prices",
        "paper_schema_metadata",
        "paper_accounts",
        "paper_runs",
        "paper_events",
        "paper_state",
    }
    assert version == (1,)
    assert daily_prices == [(1,)]


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
