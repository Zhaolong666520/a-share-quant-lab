from pathlib import Path

import duckdb
import pytest

from finance_lab.paper_store import PaperStore, PaperStoreError
from finance_lab.recovery_rebuild import _ledger
from tests.test_paper_store import _accounts_and_initial_steps


def populated(path: Path) -> None:
    a, b, first, second, context = _accounts_and_initial_steps()
    with PaperStore(path) as store:
        store.ensure_schema()
        store.initialize_portfolio((a, b), (first, second), context, batch_id="schema")


def legacy(path: Path) -> None:
    populated(path)
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE market_sentinel AS SELECT 42 AS n")
        connection.execute("UPDATE paper_schema_metadata SET schema_version = 1")
        # Preserve an authentic v1 shape and all its financial rows.
        ddl = connection.execute(
            "SELECT sql FROM duckdb_tables() WHERE table_name = 'paper_state'"
        ).fetchone()[0]
        connection.execute("ALTER TABLE paper_state RENAME TO state_buffer")
        ddl = ddl.replace("distribution_entitlements_json VARCHAR DEFAULT('[]') NOT NULL, ", "")
        ddl = ddl.replace("distribution_entitlements_json VARCHAR DEFAULT('[]'), ", "")
        # Removing through the catalog is more reliable than assuming DDL display spacing.
        connection.execute(ddl)
        if "distribution_entitlements_json" in {
            r[1] for r in connection.execute("PRAGMA table_info('paper_state')").fetchall()
        }:
            connection.execute("ALTER TABLE paper_state DROP COLUMN distribution_entitlements_json")
        names = [r[1] for r in connection.execute("PRAGMA table_info('paper_state')").fetchall()]
        columns = ", ".join(names)
        connection.execute(f"INSERT INTO paper_state SELECT {columns} FROM state_buffer")
        connection.execute("DROP TABLE state_buffer")


def rows(path: Path) -> dict:
    with duckdb.connect(str(path), read_only=True) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            for table in ("paper_accounts", "paper_runs", "paper_events", "market_sentinel")
        }


def test_nonempty_v1_migration_preserves_events_states_and_is_idempotent(tmp_path):
    path = tmp_path / "ledger.duckdb"
    legacy(path)
    before = rows(path)
    with duckdb.connect(str(path), read_only=True) as connection:
        state_before = connection.execute(
            "SELECT account_id, cash, shares, equity, last_event_hash FROM paper_state ORDER BY ALL"
        ).fetchall()
    with PaperStore(path) as store:
        store.ensure_schema()
    assert rows(path) == before
    with duckdb.connect(str(path), read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT account_id, cash, shares, equity, last_event_hash "
                "FROM paper_state ORDER BY ALL"
            ).fetchall()
            == state_before
        )
    audit, hashes = _ledger(path)
    assert audit["status"] == "passed"
    with PaperStore(path) as store:
        store.ensure_schema()
    assert _ledger(path) == (audit, hashes)


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE paper_accounts ALTER COLUMN created_market_date TYPE VARCHAR",
        "ALTER TABLE paper_events ALTER COLUMN payload_json DROP NOT NULL",
        "ALTER TABLE paper_state ALTER COLUMN distribution_entitlements_json SET DEFAULT 'bad'",
        "ALTER TABLE paper_runs RENAME TO old_runs; "
        "CREATE TABLE paper_runs AS SELECT * FROM old_runs",
        "ALTER TABLE paper_state ADD COLUMN unknown_content VARCHAR",
        "UPDATE paper_schema_metadata SET schema_version = 0",
        "UPDATE paper_schema_metadata SET schema_version = 99",
        "DROP TABLE paper_events",
        "DROP TABLE paper_schema_metadata",
        "DELETE FROM paper_schema_metadata",
    ],
)
def test_current_malformed_schema_rejected_without_mutation(tmp_path, sql):
    path = tmp_path / "ledger.duckdb"
    populated(path)
    with duckdb.connect(str(path)) as connection:
        connection.execute(sql)
    before = path.read_bytes()
    with PaperStore(path) as store, pytest.raises(PaperStoreError):
        store.ensure_schema()
    assert path.read_bytes() == before


def test_interrupted_nonempty_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "ledger.duckdb"
    legacy(path)
    before = rows(path)
    with PaperStore(path) as store:

        def fail():
            raise RuntimeError("injected failure after DDL")

        monkeypatch.setattr(store, "_validate_schema", fail)
        with pytest.raises(RuntimeError):
            store.ensure_schema()
    assert rows(path) == before
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute(
            "SELECT schema_version FROM paper_schema_metadata"
        ).fetchall() == [(1,)]
        assert "distribution_entitlements_json" not in {
            r[1] for r in connection.execute("PRAGMA table_info('paper_state')").fetchall()
        }
    with PaperStore(path) as store:
        store.ensure_schema()
    assert _ledger(path)[0]["status"] == "passed"


def test_legacy_wrong_state_type_cannot_be_silently_cast(tmp_path):
    path = tmp_path / "ledger.duckdb"
    legacy(path)
    with duckdb.connect(str(path)) as connection:
        connection.execute("ALTER TABLE paper_state ALTER COLUMN last_close TYPE VARCHAR")
    before = path.read_bytes()
    with PaperStore(path) as store, pytest.raises(PaperStoreError):
        store.ensure_schema()
    assert path.read_bytes() == before


def test_migration_corrupt_event_chain_rolls_back(tmp_path):
    path = tmp_path / "ledger.duckdb"
    legacy(path)
    with duckdb.connect(str(path)) as connection:
        connection.execute("UPDATE paper_events SET event_hash = 'corrupted'")
    before = path.read_bytes()
    with PaperStore(path) as store, pytest.raises(PaperStoreError):
        store.ensure_schema()
    assert path.read_bytes() == before
