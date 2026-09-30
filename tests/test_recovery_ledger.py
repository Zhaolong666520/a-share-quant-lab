from pathlib import Path

import duckdb
import pytest

from finance_lab.paper_store import PaperStore, PaperStoreError
from finance_lab.recovery import prepare_recovery, verify_recovery
from tests.test_paper_store import _accounts_and_initial_steps


def test_empty_ledger_audit_is_read_only(tmp_path: Path) -> None:
    database = tmp_path / "ledger.duckdb"
    with PaperStore(database) as store:
        store.ensure_schema()
    before = database.read_bytes()
    with PaperStore(database, read_only=True) as store:
        result = store.audit_recovery_ledger()
    assert result == {"status": "passed", "accounts": [], "event_count": 0}
    assert database.read_bytes() == before


def test_market_only_is_not_a_ledger(tmp_path: Path) -> None:
    database = tmp_path / "ledger.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE daily_prices (n INTEGER)")
    with PaperStore(database, read_only=True) as store:
        assert store.audit_recovery_ledger()["status"] == "not_present"


@pytest.mark.parametrize("version", [1, 3])
def test_unsupported_schema_is_not_migrated(tmp_path: Path, version: int) -> None:
    database = tmp_path / "ledger.duckdb"
    with PaperStore(database) as store:
        store.ensure_schema()
    with duckdb.connect(str(database)) as connection:
        connection.execute("UPDATE paper_schema_metadata SET schema_version = ?", [version])
    before = database.read_bytes()
    with PaperStore(database, read_only=True) as store:
        assert store.audit_recovery_ledger()["status"] == "unsupported"
    assert database.read_bytes() == before


def test_partial_schema_cannot_be_treated_as_empty(tmp_path: Path) -> None:
    database = tmp_path / "ledger.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE paper_events (n INTEGER)")
    with PaperStore(database, read_only=True) as store, pytest.raises(PaperStoreError):
        store.audit_recovery_ledger()


def test_uppercase_partial_schema_is_not_absent(tmp_path: Path) -> None:
    database = tmp_path / "ledger.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE PAPER_EVENTS (n INTEGER)")
    with PaperStore(database, read_only=True) as store, pytest.raises(PaperStoreError):
        store.audit_recovery_ledger()


def test_audit_requires_read_only_connection(tmp_path: Path) -> None:
    with PaperStore(tmp_path / "ledger.duckdb") as store, pytest.raises(PaperStoreError):
        store.audit_recovery_ledger()


@pytest.fixture
def populated_database(tmp_path: Path) -> Path:
    database = tmp_path / "ledger.duckdb"
    a, b, first, second, context = _accounts_and_initial_steps()
    with PaperStore(database) as store:
        store.ensure_schema()
        store.initialize_portfolio((a, b), (first, second), context, batch_id="test")
    return database


def test_nonempty_recovery_audit_preserves_all_bytes(populated_database: Path) -> None:
    before = populated_database.read_bytes()
    with PaperStore(populated_database, read_only=True) as store:
        result = store.audit_recovery_ledger()
        states = store.audit_portfolio("default")
    assert result["status"] == "passed"
    assert len(result["accounts"]) == 2
    assert result["event_count"] > 0
    by_id = {item["account_id"]: item for item in result["accounts"]}
    for state in states:
        assert by_id[state.account_id]["cash"] == state.cash
        assert by_id[state.account_id]["shares"] == state.shares
        assert by_id[state.account_id]["equity"] == state.equity
        assert by_id[state.account_id]["last_event_hash"] == state.last_event_hash
    assert populated_database.read_bytes() == before


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM paper_accounts",
        "DELETE FROM paper_runs",
        "UPDATE paper_state SET cash = cash + 1",
        "UPDATE paper_events SET event_hash = 'bad'",
        "ALTER TABLE paper_state DROP COLUMN distribution_entitlements_json",
        "CREATE TABLE paper_unknown (n INTEGER)",
    ],
)
def test_corrupt_ledger_is_not_hidden(populated_database: Path, sql: str) -> None:
    with duckdb.connect(str(populated_database)) as connection:
        connection.execute(sql)
    before = populated_database.read_bytes()
    with PaperStore(populated_database, read_only=True) as store, pytest.raises(PaperStoreError):
        store.audit_recovery_ledger()
    assert populated_database.read_bytes() == before


def test_nonempty_database_copy_roundtrip(populated_database: Path, tmp_path: Path) -> None:
    source = tmp_path / "project"
    (source / "data").mkdir(parents=True)
    database = source / "data/finance_lab.duckdb"
    before = populated_database.read_bytes()
    database.write_bytes(before)
    target = tmp_path / "bundle"
    prepared = prepare_recovery(source, target)
    verified = verify_recovery(target)
    assert prepared["ledger_audit"] == verified["ledger_audit"]
    assert len(verified["ledger_audit"]["accounts"]) == 2
    assert verified["ledger_audit"]["event_count"] > 0
    assert database.read_bytes() == before
    assert (target / "snapshot/data/finance_lab.duckdb").read_bytes() == before
