from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from finance_lab.paper_pipeline import paper_init_portfolio, paper_run_portfolio
from tests.paper_helpers import make_paper_test_project
from tests.test_paper_pipeline import _append_rising_bars
from tests.test_paper_run_identity import automation


def acceptance():
    import importlib.util

    assert importlib.util.find_spec("finance_lab.paper_run_acceptance") is not None, (
        "Missing read-only run acceptance API"
    )
    return importlib.import_module("finance_lab.paper_run_acceptance")


def initialized(root):
    paths, prices = make_paper_test_project(root, rising_prices=True)
    paper_init_portfolio(root=root, as_of_date=prices.trade_date.max().date())
    return paths, prices


def test_real_interval_preserves_history_and_verifies_repeat_noop(tmp_path):
    api = acceptance()
    root = tmp_path / "synthetic"
    root.mkdir()
    paths, prices = initialized(root)
    baseline = api.capture_run_acceptance(root)
    evidence = tmp_path / "before.json"
    evidence.write_text(json.dumps(baseline))
    _, day = _append_rising_bars(paths, prices, 2)
    first = automation(root, day, paper_run_portfolio)
    repeat = automation(root, day, paper_run_portfolio)
    result = api.verify_run_acceptance(
        root, "default", evidence, [first.payload["attempt_id"], repeat.payload["attempt_id"]]
    )
    assert result["status"] == "passed", result
    assert result["new_run_count"] == 4
    assert result["checks"]["repeat_noop_verified"] is True
    assert result["attempts"][1]["correlation"]["status"] == "no_new_commits"
    assert result["safety"]["non_atomic"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "root",
        "portfolio",
        "bool",
        "extra",
        "unsorted",
        "duplicate",
        "control",
        "state_roster",
        "date",
        "nan",
    ],
)
def test_baseline_protocol_rejects_malformed_values(tmp_path, mutation):
    api = acceptance()
    from finance_lab.paper_acceptance_protocol import validate_baseline

    root = tmp_path / "synthetic"
    root.mkdir()
    initialized(root)
    baseline = api.capture_run_acceptance(root)
    root_id = baseline["root_id"]
    if mutation == "hash":
        baseline["baseline_id"] = "0" * 64
    elif mutation == "root":
        baseline["root_id"] = "0" * 64
    elif mutation == "portfolio":
        baseline["portfolio_id"] = "other"
    elif mutation == "bool":
        baseline["schema_version"] = True
    elif mutation == "extra":
        baseline["extra"] = None
    elif mutation == "unsorted":
        baseline["ledger"]["paper_accounts"].reverse()
    elif mutation == "duplicate":
        baseline["ledger"]["paper_accounts"].append(baseline["ledger"]["paper_accounts"][0])
    elif mutation == "control":
        baseline["ledger"]["paper_runs"][0]["run_id"] = "bad\nidentifier"
    elif mutation == "state_roster":
        baseline["ledger"]["paper_state"].pop()
    elif mutation == "date":
        baseline["captured_at"] = "20260101T000000Z"
    else:
        baseline["ledger"]["paper_runs"][0]["row_sha256"] = float("nan")
    if mutation not in {"hash", "nan"}:
        from finance_lab.paper_acceptance_protocol import baseline_digest

        baseline["baseline_id"] = baseline_digest(baseline)
    with pytest.raises(api.PaperAcceptanceRequestError):
        validate_baseline(baseline, root_id, "default")


@pytest.fixture
def interval(tmp_path):
    api = acceptance()
    root = tmp_path / "synthetic"
    root.mkdir()
    paths, prices = initialized(root)
    baseline = api.capture_run_acceptance(root)
    evidence = tmp_path / "before.json"
    evidence.write_text(json.dumps(baseline))
    _, day = _append_rising_bars(paths, prices, 2)
    first = automation(root, day, paper_run_portfolio)
    repeat = automation(root, day, paper_run_portfolio)
    return api, root, paths, prices, baseline, evidence, first, repeat


def verify(interval, ids=None):
    api, root, _, _, _, evidence, first, repeat = interval
    return api.verify_run_acceptance(
        root,
        "default",
        evidence,
        ids if ids is not None else [first.payload["attempt_id"], repeat.payload["attempt_id"]],
    )


def rewrite_archive(result, value):
    from tests.test_paper_automation_history import seal

    value = seal(value)
    result.archive_path.unlink()
    stamp = datetime.fromisoformat(value["started_at"].replace("Z", "+00:00"))
    path = result.archive_path.parent / (
        f"default_{stamp.strftime('%Y%m%dT%H%M%S%fZ')}_{value['run_id']}.json"
    )
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize("selection", ["omit", "reverse", "duplicate", "unknown"])
def test_attempt_selection_is_exact_full_interval(interval, selection):
    first, repeat = interval[-2:]
    ids = [first.payload["attempt_id"], repeat.payload["attempt_id"]]
    if selection == "omit":
        ids.pop()
    elif selection == "reverse":
        ids.reverse()
    elif selection == "duplicate":
        ids.append(ids[0])
    else:
        ids[0] = "a" * 32
    result = verify(interval, ids)
    assert result["status"] == "attention"
    assert result["checks"]["archive_interval_complete"] is False


@pytest.mark.parametrize(
    "column",
    [
        "paper_runs.started_at",
        "paper_events.created_at",
        "paper_accounts.created_at",
        "paper_schema_metadata.applied_at",
    ],
)
def test_nonbusiness_old_row_mutation_is_detected_even_when_audit_passes(interval, column):
    import duckdb

    from finance_lab.paper_store import PaperStore

    table, field = column.split(".")
    with duckdb.connect(str(interval[2].database)) as connection:
        connection.execute(f"UPDATE {table} SET {field} = {field} + INTERVAL '1 second'")
    with PaperStore(interval[2].database, read_only=True) as store:
        assert store.audit_recovery_ledger()["status"] == "passed"
    result = verify(interval)
    assert result["status"] == "attention"
    assert result["checks"]["history_preserved"] is False


def test_unrecorded_commits_are_not_inferred_as_rollback(interval):
    api, root, paths, prices, _, _, _, _ = interval
    updated, _ = _append_rising_bars(paths, prices, 2)
    _, day = _append_rising_bars(paths, updated, 1)
    paper_run_portfolio(root=root, as_of_date=day)
    result = verify(interval)
    assert result["status"] == "attention"
    assert result["new_run_count"] == 6
    assert "unclaimed_commits" in {d["code"] for d in result["diagnostics"]}


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_claim",
        "wrong_field",
        "missing_account",
        "same_time",
        "before",
        "future",
        "schema1",
        "invalid_new",
    ],
)
def test_new_archive_cannot_hide_bad_or_ambiguous_evidence(interval, mutation):
    first, repeat = interval[-2:]
    value = json.loads(json.dumps(first.payload))
    if mutation == "duplicate_claim":
        value = json.loads(json.dumps(first.payload))
        value.update(
            attempt_id=repeat.payload["attempt_id"],
            started_at=repeat.payload["started_at"],
            completed_at=repeat.payload["completed_at"],
        )
        rewrite_archive(repeat, value)
    elif mutation == "wrong_field":
        value["run_identity"]["ledger"]["runs"][0]["state_hash"] = "0" * 64
        rewrite_archive(first, value)
    elif mutation == "missing_account":
        value["run_identity"]["ledger"]["runs"].pop()
        rewrite_archive(first, value)
    elif mutation == "same_time":
        value = json.loads(json.dumps(repeat.payload))
        value["started_at"] = first.payload["started_at"]
        rewrite_archive(repeat, value)
    elif mutation == "before":
        value["started_at"] = (
            (
                datetime.fromisoformat(interval[4]["captured_at"].replace("Z", "+00:00"))
                - timedelta(seconds=1)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        rewrite_archive(first, value)
    elif mutation == "future":
        value["completed_at"] = (
            (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        )
        rewrite_archive(first, value)
    elif mutation == "schema1":
        value["schema_version"] = 1
        value.pop("attempt_id")
        value.pop("run_identity")
        rewrite_archive(first, value)
    else:
        (
            first.archive_path.parent / "default_20990101T000000000000Z_aaaaaaaaaaaaaaaa.json"
        ).write_text("{}")
    result = verify(interval)
    assert result["status"] == "attention", result
    assert result["checks"]["repeat_noop_verified"] is False


def test_capture_keeps_invalid_old_archive_bytes_and_verify_protects_them(tmp_path):
    api = acceptance()
    root = tmp_path / "synthetic"
    root.mkdir()
    paths, prices = initialized(root)
    directory = root / "outputs" / "paper_automation_runs"
    directory.mkdir()
    old = directory / "default_20000101T000000000000Z_aaaaaaaaaaaaaaaa.json"
    old.write_bytes(b"not JSON: deliberately retained old evidence")
    baseline = api.capture_run_acceptance(root)
    evidence = tmp_path / "before.json"
    evidence.write_text(json.dumps(baseline))
    _, day = _append_rising_bars(paths, prices, 2)
    first = automation(root, day, paper_run_portfolio)
    repeat = automation(root, day, paper_run_portfolio)
    ids = [first.payload["attempt_id"], repeat.payload["attempt_id"]]
    assert api.verify_run_acceptance(root, "default", evidence, ids)["status"] == "passed"
    old.write_bytes(b"changed old bytes")
    assert api.verify_run_acceptance(root, "default", evidence, ids)["status"] == "attention"


@pytest.mark.parametrize("operation", ["capture", "verify"])
def test_wal_blocks_before_any_connection(interval, monkeypatch, operation):
    from finance_lab.operational_status_io import EvidenceError
    from finance_lab.paper_store import PaperStore

    api, root, paths = interval[:3]
    paths.database.with_name(paths.database.name + ".wal").write_bytes(b"synthetic WAL")

    def forbidden(self):
        pytest.fail("WAL must stop before opening any database connection")

    monkeypatch.setattr(PaperStore, "__enter__", forbidden)
    if operation == "capture":
        with pytest.raises(EvidenceError, match="ledger_wal_present"):
            api.capture_run_acceptance(root)
    else:
        result = verify(interval)
        assert result["status"] == "unavailable"
        assert result["new_run_count"] is None


def test_changed_input_downgrades_previously_passed_result(interval, monkeypatch):
    from finance_lab.operational_status_io import EvidenceError, Observation

    monkeypatch.setattr(Observation, "changed", lambda self: {"ledger"})
    result = verify(interval)
    assert result["status"] == "unavailable"
    assert result["checks"]["repeat_noop_verified"] is False
    assert result["observation"]["input_changed"] is True
    with pytest.raises(EvidenceError, match="input_changed"):
        interval[0].capture_run_acceptance(interval[1])


@pytest.mark.parametrize(
    "raw",
    [
        b'{"baseline_id": "x", "baseline_id": "y"}',
        b'{"x": NaN}',
        b'{"x": 1e999}',
        b"\xef\xbb\xbf{}",
    ],
)
def test_baseline_json_is_strict_request_error(interval, raw):
    api = interval[0]
    interval[5].write_bytes(raw)
    with pytest.raises(api.PaperAcceptanceRequestError, match="baseline_invalid"):
        verify(interval)


def test_verify_cli_exit_status_and_request_redaction(interval, capsys):
    api, root, _, _, _, evidence, first, repeat = interval
    arguments = ["verify", "--root", str(root), "--baseline", str(evidence)]
    ids = [first.payload["attempt_id"], repeat.payload["attempt_id"]]
    assert api.main(arguments + ["--attempt-id", ids[0], "--attempt-id", ids[1]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    assert api.main(arguments + ["--attempt-id", ids[0]]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "attention"
    private = "PRIVATE-PATH\nBAD"
    assert api.main(arguments + ["--portfolio", private]) == 2
    captured = capsys.readouterr()
    assert private not in captured.out + captured.err
    assert str(root) not in captured.out


def test_relative_baseline_checks_ancestors_and_readonly_baseline_bounds(interval, monkeypatch):
    # A relative filename should be normalized before Observation, so all absolute
    # ancestors receive the same safety checks as root/database/archive inputs.
    from finance_lab.operational_status_io import Observation

    captured = []
    original = Observation.read

    def read(self, path, section, **kwargs):
        if section == "baseline":
            captured.append(path)
        return original(self, path, section, **kwargs)

    monkeypatch.setattr(Observation, "read", read)
    monkeypatch.chdir(interval[5].parent)
    interval[0].verify_run_acceptance(interval[1], "default", Path("before.json"), [])
    assert captured and captured[0].is_absolute()


def test_untrusted_view_is_not_queried_before_schema_check(interval, monkeypatch):
    import duckdb

    from finance_lab.operational_status_io import EvidenceError
    from finance_lab.paper_store import PaperStore

    with duckdb.connect(str(interval[2].database)) as connection:
        connection.execute("ALTER TABLE paper_events RENAME TO original_events")
        connection.execute("CREATE VIEW paper_events AS SELECT * FROM original_events")
    queries = []
    original = PaperStore._require_connection

    class Recorder:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            queries.append(sql)
            return self.connection.execute(sql, *args)

    monkeypatch.setattr(PaperStore, "_require_connection", lambda self: Recorder(original(self)))
    with pytest.raises(EvidenceError):
        interval[0].capture_run_acceptance(interval[1])
    assert not any("SELECT 1 FROM paper_events" in query for query in queries)


def test_parent_traversal_is_request_error_not_evidence_error(interval, capsys):
    api, root = interval[:2]
    assert api.main(["capture", "--root", str(root / ".." / "synthetic")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_cli_parser_errors_use_fixed_stdout_json(capsys):
    api = acceptance()
    private = "PRIVATE-EXTERNAL-PATH"
    assert api.main(["capture", "--root", ".", "--unknown", private]) == 2
    captured = capsys.readouterr()
    assert not captured.err
    assert private not in captured.out
    assert json.loads(captured.out)["diagnostics"][0]["code"] == "arguments_invalid"


def test_new_invalid_archive_filename_is_attention_not_read_failure(interval):
    directory = interval[-1].archive_path.parent
    (directory / "default_malformed_name.json").write_text("{}")
    result = verify(interval)
    assert result["status"] == "attention"
    assert "new_archive_invalid" in {d["code"] for d in result["diagnostics"]}


@pytest.mark.parametrize("scenario", ["single", "only_noop", "different_day"])
def test_repeat_noop_requires_processed_success_same_date(tmp_path, scenario):
    api = acceptance()
    root = tmp_path / "synthetic"
    root.mkdir()
    paths, prices = initialized(root)
    baseline = api.capture_run_acceptance(root)
    evidence = tmp_path / "before.json"
    evidence.write_text(json.dumps(baseline))
    ids = []
    if scenario == "only_noop":
        result = automation(root, prices.trade_date.max().date(), paper_run_portfolio)
        ids.append(result.payload["attempt_id"])
    else:
        updated, day = _append_rising_bars(paths, prices, 1)
        result = automation(root, day, paper_run_portfolio)
        ids.append(result.payload["attempt_id"])
        if scenario == "different_day":
            result = automation(root, day, paper_run_portfolio)
            # Exercise date policy independently from coverage: a valid no-op
            # archive on a different observation date still claims no rows.
            value = json.loads(json.dumps(result.payload))
            value["as_of_date"] = (day + timedelta(days=1)).isoformat()
            value["run_identity"]["health"]["as_of_date"] = value["as_of_date"]
            rewrite_archive(result, value)
            ids.append(result.payload["attempt_id"])
    verified = api.verify_run_acceptance(root, "default", evidence, ids)
    assert verified["status"] == "attention"
    assert verified["checks"]["repeat_noop_verified"] is False
    if scenario == "different_day":
        assert verified["checks"]["exact_run_coverage"] is True
        assert verified["checks"]["all_attempts_successful"] is True


@pytest.mark.parametrize("failure", ["report", "partial", "commit_without_receipt", "upstream"])
def test_failure_preserves_known_receipts_and_never_passes(tmp_path, monkeypatch, failure):
    import finance_lab.paper_pipeline as pipeline
    from finance_lab.paper_automation import PaperAutomationActions, run_paper_daily_automation
    from finance_lab.paper_store import PaperStore

    api = acceptance()
    root = tmp_path / "synthetic"
    root.mkdir()
    paths, prices = initialized(root)
    evidence = tmp_path / "before.json"
    evidence.write_text(json.dumps(api.capture_run_acceptance(root)))
    _, day = _append_rising_bars(paths, prices, 2)

    def fail(*args, **kwargs):
        raise RuntimeError("private synthetic failure")

    if failure == "report":
        monkeypatch.setattr(pipeline, "write_paper_report", fail)
        result = automation(root, day, paper_run_portfolio)
    elif failure == "partial":
        original = PaperStore.commit_batch_with_receipt
        calls = 0

        def commit(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                fail()
            return original(self, *args, **kwargs)

        monkeypatch.setattr(PaperStore, "commit_batch_with_receipt", commit)
        result = automation(root, day, paper_run_portfolio)
    elif failure == "commit_without_receipt":

        def run(**kwargs):
            paper_run_portfolio(**kwargs)
            fail()

        result = automation(root, day, run)
    else:
        result = run_paper_daily_automation(
            root=root,
            as_of_date=day,
            actions=PaperAutomationActions(fail, fail, fail, fail),
        )
    verified = api.verify_run_acceptance(root, "default", evidence, [result.payload["attempt_id"]])
    assert verified["status"] == "attention", verified
    record = verified["attempts"][0]
    if failure == "report":
        assert verified["new_run_count"] == 4
        assert record["correlation"]["status"] == "verified"
        assert record["state"] == "failed_after_commit"
    elif failure == "partial":
        assert verified["new_run_count"] == 2
        assert record["correlation"]["status"] == "partial_verified"
    else:
        assert record["run_identity"]["ledger"]["scope"] == "unknown"
        assert record["correlation"]["status"] == "not_available"
        if failure == "commit_without_receipt":
            assert verified["new_run_count"] == 4
            assert "unclaimed_commits" in {d["code"] for d in verified["diagnostics"]}


def test_unreadable_archives_leave_both_counts_unknown(interval, monkeypatch):
    from finance_lab.operational_status_io import EvidenceError, Observation

    original = Observation.listing

    def listing(self, path, section, prefix=None):
        if section == "archives":
            raise EvidenceError("path_unreadable")
        return original(self, path, section, prefix)

    monkeypatch.setattr(Observation, "listing", listing)
    result = verify(interval)
    assert result["status"] == "unavailable"
    assert result["new_run_count"] is None
    assert result["recorded_attempt_count"] is None


def test_row_archive_and_baseline_limits_fail_closed(interval, monkeypatch):
    import finance_lab.operational_status_io as io
    import finance_lab.paper_acceptance_protocol as protocol
    from finance_lab.operational_status_io import EvidenceError

    api, root = interval[:2]
    with monkeypatch.context() as patch:
        patch.setattr(api, "MAX_TABLE_ROWS", 1)
        with pytest.raises(EvidenceError, match="ledger_row_limit"):
            api.capture_run_acceptance(root)
    with monkeypatch.context() as patch:
        patch.setattr(io, "MAX_ARCHIVES", 1)
        with pytest.raises(EvidenceError, match="archive_limit"):
            api.capture_run_acceptance(root)
    with monkeypatch.context() as patch:
        patch.setattr(api, "MAX_BASELINE_BYTES", 10)
        with pytest.raises(EvidenceError, match="json_too_large"):
            verify(interval)
    with monkeypatch.context() as patch:
        patch.setattr(protocol, "MAX_BASELINE_BYTES", 10)
        with pytest.raises(EvidenceError):
            api.capture_run_acceptance(root)


def test_symlink_evidence_rejected(interval):
    from finance_lab.operational_status_io import EvidenceError

    api, root = interval[:2]
    linked = root.parent / "linked-synthetic"
    try:
        linked.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink creation not permitted; reparse guard tested separately")
    with pytest.raises(EvidenceError, match="path_unsafe"):
        api.capture_run_acceptance(linked)


def test_windows_reparse_ancestor_rejected(interval, monkeypatch):
    from types import SimpleNamespace

    from finance_lab.operational_status_io import EvidenceError

    api, root = interval[:2]
    original = Path.lstat

    def lstat(path):
        info = original(path)
        if path == root.parent:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(EvidenceError, match="path_unsafe"):
        api.capture_run_acceptance(root)


def test_readonly_capture_verify_import_and_write_fences(interval):
    api, root, _, _, _, evidence, first, repeat = interval
    fingerprints = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    script = r"""
import builtins, json, os, sys
from pathlib import Path
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    forbidden = {"finance_lab.paper_automation", "finance_lab.paper_pipeline",
                 "finance_lab.pipeline", "finance_lab.paper_report"}
    if name in forbidden or name.startswith("matplotlib"):
        raise RuntimeError("writer/plot import forbidden")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from finance_lab.paper_run_acceptance import capture_run_acceptance, verify_run_acceptance
def audit(event, args):
    if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.symlink"}:
        raise RuntimeError("filesystem mutation forbidden")
    if event == "open":
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
            raise RuntimeError("write open forbidden")
sys.addaudithook(audit)
root, evidence = Path(sys.argv[1]), Path(sys.argv[2])
capture_run_acceptance(root)
value = verify_run_acceptance(root, "default", evidence, sys.argv[3:])
assert value["status"] == "passed", value
assert not any(name in sys.modules for name in (
    "finance_lab.paper_automation", "finance_lab.paper_pipeline", "finance_lab.pipeline",
    "finance_lab.paper_report", "matplotlib"))
print(json.dumps({"status": value["status"]}))
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(root),
            str(evidence),
            first.payload["attempt_id"],
            repeat.payload["attempt_id"],
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert json.loads(completed.stdout)["status"] == "passed"
    assert fingerprints == {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_baseline_run_references_cannot_be_replayed_as_new_commits(interval):
    from finance_lab.paper_store import PaperStore

    api, root, paths, _, baseline, _, first, _ = interval
    old_ids = {r["run_id"] for r in baseline["ledger"]["paper_runs"]}
    with PaperStore(paths.database, read_only=True) as store:
        rows = (
            store._require_connection()
            .execute(
                "SELECT run_id, batch_id, account_id, trade_date, dataset_id, curated_sha256, "
                "state_hash FROM paper_runs ORDER BY trade_date, account_id"
            )
            .fetchall()
        )
    fields = [
        "run_id",
        "batch_id",
        "account_id",
        "trade_date",
        "dataset_id",
        "curated_sha256",
        "state_hash",
    ]
    refs = [
        dict(
            zip(fields, [v.isoformat() if hasattr(v, "isoformat") else v for v in row], strict=True)
        )
        for row in rows
        if row[0] in old_ids
    ]
    value = json.loads(json.dumps(first.payload))
    value["processed_dates"] = sorted({ref["trade_date"] for ref in refs})
    value["run_identity"]["ledger"]["runs"] = refs
    rewrite_archive(first, value)
    result = verify(interval)
    assert result["status"] == "attention"
    assert "baseline_run_reused" in {d["code"] for d in result["diagnostics"]}


def test_other_portfolio_cannot_hide_new_real_commits(interval):
    root = interval[1]
    day = datetime.fromisoformat(interval[-2].payload["as_of_date"]).date()
    paper_init_portfolio(root=root, portfolio_id="other", as_of_date=day)
    result = verify(interval)
    assert result["status"] == "attention"
    assert result["new_run_count"] == 6
    assert result["checks"]["history_preserved"] is False
    assert "unclaimed_commits" in {d["code"] for d in result["diagnostics"]}


def test_deleted_history_never_passes_even_if_entire_account_is_removed(interval):
    import duckdb

    api, root, paths = interval[:3]
    with duckdb.connect(str(paths.database)) as connection:
        for table in ("paper_events", "paper_state", "paper_runs", "paper_accounts"):
            connection.execute(f"DELETE FROM {table}")
    result = verify(interval)
    assert result["status"] == "unavailable"
    assert result["new_run_count"] is None


def test_empty_audited_tables_do_not_capture_missing_portfolio(tmp_path):
    from finance_lab.operational_status_io import EvidenceError
    from finance_lab.paper_store import PaperStore

    api = acceptance()
    root = tmp_path / "synthetic"
    database = root / "data" / "finance_lab.duckdb"
    database.parent.mkdir(parents=True)
    with PaperStore(database) as store:
        store.ensure_schema()
    with pytest.raises(EvidenceError, match="ledger_portfolio_missing"):
        api.capture_run_acceptance(root)
