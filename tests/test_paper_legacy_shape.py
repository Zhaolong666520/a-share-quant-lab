"""Legacy shape classification on newly constructed synthetic ledgers only."""

from itertools import combinations
from pathlib import Path

import duckdb
import pytest

from finance_lab.paper_store import PaperStore, PaperStoreError
from tests.test_paper_schema_contract import legacy, populated

BUSINESS_TABLES = ("paper_accounts", "paper_runs", "paper_events", "paper_state")
UNSUPPORTED_SHAPES = [
    kept
    for size in (1, 2, 3)
    for kept in combinations(BUSINESS_TABLES, size)
    if kept != ("paper_state",)
]


def snapshot(path: Path) -> dict:
    with duckdb.connect(str(path), read_only=True) as connection:
        objects = connection.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        columns = connection.execute(
            "SELECT table_name, column_name, ordinal_position, data_type, is_nullable, "
            "column_default FROM information_schema.columns WHERE table_schema = 'main' "
            "ORDER BY table_name, ordinal_position"
        ).fetchall()
        constraints = connection.execute(
            "SELECT table_name, constraint_type, constraint_text, constraint_column_names "
            "FROM duckdb_constraints() WHERE schema_name = 'main' "
            "ORDER BY table_name, constraint_type, constraint_text"
        ).fetchall()
        rows = {
            name: connection.execute(f'SELECT * FROM "{name}" ORDER BY ALL').fetchall()
            for name, _ in objects
        }
    return {"objects": objects, "columns": columns, "constraints": constraints, "rows": rows}


@pytest.mark.parametrize("empty", [False, True], ids=["populated", "empty"])
@pytest.mark.parametrize("kept", UNSUPPORTED_SHAPES, ids=lambda kept: "+".join(kept))
def test_unsupported_v1_subset_rejected_without_repair(tmp_path, kept, empty):
    path = tmp_path / "synthetic.duckdb"
    legacy(path)
    with duckdb.connect(str(path)) as connection:
        if empty:
            for name in BUSINESS_TABLES:
                connection.execute(f'DELETE FROM "{name}"')
        for name in BUSINESS_TABLES:
            if name not in kept:
                connection.execute(f'DROP TABLE "{name}"')
    before = snapshot(path)
    failure = None
    with PaperStore(path) as store:
        try:
            store.ensure_schema()
        except Exception as exc:
            failure = exc
    assert snapshot(path) == before, "Rejected subset must not add tables, fields or versions"
    assert isinstance(failure, PaperStoreError), repr(failure)


@pytest.mark.parametrize("missing", BUSINESS_TABLES)
def test_current_version_missing_table_remains_rejected(tmp_path, missing):
    path = tmp_path / "synthetic.duckdb"
    populated(path)
    with duckdb.connect(str(path)) as connection:
        connection.execute(f'DROP TABLE "{missing}"')
    before = snapshot(path)
    with PaperStore(path) as store, pytest.raises(PaperStoreError):
        store.ensure_schema()
    assert snapshot(path) == before


@pytest.mark.parametrize("version", [None, 1])
def test_metadata_only_initialization_remains_compatible(tmp_path, version):
    path = tmp_path / "synthetic.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE market_sentinel AS SELECT 42 AS n")
        connection.execute(
            "CREATE TABLE paper_schema_metadata (schema_version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        if version is not None:
            connection.execute(
                "INSERT INTO paper_schema_metadata VALUES (?, CURRENT_TIMESTAMP)", [version]
            )
    with PaperStore(path) as store:
        store.ensure_schema()
        store.ensure_schema()
    after = snapshot(path)
    assert after["rows"]["market_sentinel"] == [(42,)]
    assert [row[0] for row in after["rows"]["paper_schema_metadata"]] == (
        [2] if version is None else [1, 2]
    )
    assert all(after["rows"][name] == [] for name in BUSINESS_TABLES)
    with PaperStore(path, read_only=True) as store:
        assert store.audit_recovery_ledger()["status"] == "passed"


def test_state_only_compatibility_cannot_prove_complete_history(tmp_path):
    path = tmp_path / "synthetic.duckdb"
    legacy(path)
    with duckdb.connect(str(path)) as connection:
        for name in BUSINESS_TABLES:
            if name != "paper_state":
                connection.execute(f'DROP TABLE "{name}"')
    before = snapshot(path)
    with PaperStore(path) as store:
        store.ensure_schema()
    with duckdb.connect(str(path), read_only=True) as connection:
        old_columns = [row[1] for row in before["columns"] if row[0] == "paper_state"]
        selected = ", ".join('"' + name + '"' for name in old_columns)
        assert (
            connection.execute(f"SELECT {selected} FROM paper_state ORDER BY ALL").fetchall()
            == (before["rows"]["paper_state"])
        )
        assert connection.execute("SELECT n FROM market_sentinel").fetchall() == [(42,)]
    after = snapshot(path)
    with PaperStore(path, read_only=True) as store, pytest.raises(PaperStoreError):
        store.audit_recovery_ledger()
    assert snapshot(path) == after


@pytest.mark.parametrize("stage", ["after_ddl", "after_audit"])
def test_full_legacy_migration_failure_rolls_back_all_structure_and_rows(
    tmp_path, monkeypatch, stage
):
    path = tmp_path / "synthetic.duckdb"
    legacy(path)
    before = snapshot(path)
    observed = []
    with PaperStore(path) as store:
        method = "_ensure_schema_objects" if stage == "after_ddl" else "_audit_recovery_ledger"
        original = getattr(store, method)

        def fail_after_real_work():
            original()
            columns = store._require_connection().execute("DESCRIBE paper_state").fetchall()
            assert "distribution_entitlements_json" in {row[0] for row in columns}
            if stage == "after_audit":
                assert store._require_connection().execute(
                    "SELECT MAX(schema_version) FROM paper_schema_metadata"
                ).fetchone() == (2,)
            observed.append(stage)
            raise RuntimeError("injected after real migration work")

        monkeypatch.setattr(store, method, fail_after_real_work)
        with pytest.raises(RuntimeError, match="injected after real migration work"):
            store.ensure_schema()
    assert observed == [stage]
    assert snapshot(path) == before
