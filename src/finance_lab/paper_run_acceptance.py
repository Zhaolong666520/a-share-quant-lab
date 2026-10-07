"""Read-only recorded automation/ledger interval acceptance; no execution or publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NoReturn

from finance_lab.operational_status_io import (
    EvidenceError,
    Observation,
    diagnostic,
    safe_path,
    strict_json,
    validate_root,
)
from finance_lab.paper_acceptance_protocol import (
    MAX_BASELINE_BYTES,
    MAX_TABLE_ROWS,
    TABLE_IDENTIFIERS,
    PaperAcceptanceRequestError,
    archive_name,
    baseline_digest,
    canonical_bytes,
    hex_digest,
    identifier,
    moment,
    validate_baseline,
)
from finance_lab.paper_automation_history import _safe_record, _validate
from finance_lab.paper_models import validate_portfolio_id
from finance_lab.paper_store import PaperStore

_SAFETY = {
    "non_atomic": True,
    "broker_connected": False,
    "authorizes_real_money": False,
    "actual_exit_codes_verified": False,
    "scheduler_status": "unknown",
    "current_health_verified": False,
    "report_artifacts_verified": False,
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _request(root: Path, portfolio: str) -> tuple[Path, str, str]:
    try:
        portfolio = validate_portfolio_id(portfolio)
    except (TypeError, ValueError) as exc:
        raise PaperAcceptanceRequestError("portfolio_id_invalid") from exc
    if not isinstance(root, Path) or ".." in root.parts:
        raise PaperAcceptanceRequestError("root_invalid")
    root = validate_root(root)
    root_id = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    return root, portfolio, root_id


def _archives(
    root: Path, portfolio: str, observer: Observation, *, validate_names: bool = True
) -> dict[str, bytes]:
    directory = root / "outputs" / "paper_automation_runs"
    result = {}
    for path in observer.listing(directory, "archives", f"{portfolio}_") or []:
        if validate_names:
            try:
                archive_name(path.name, portfolio)
            except (ValueError, TypeError) as exc:
                raise EvidenceError("archive_name_invalid") from exc
        raw = observer.read(path, "archives")
        if raw is None:
            raise EvidenceError("archive_unavailable")
        result[path.name] = raw
    return result


def _database(root: Path, observer: Observation) -> Path:
    path = root / "data" / "finance_lab.duckdb"
    wal = path.with_name(path.name + ".wal")
    observer.read(wal, "ledger", limit=None)
    if safe_path(wal) is not None:
        raise EvidenceError("ledger_wal_present")
    observer.read(path, "ledger", limit=None)
    if safe_path(path) is None:
        raise EvidenceError("ledger_missing")
    return path


def _scalar(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if type(value) is float and not math.isfinite(value):
        raise EvidenceError("ledger_nonfinite")
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise EvidenceError("ledger_scalar_invalid")


def _snapshot(store: PaperStore, portfolio: str) -> tuple[dict[str, Any], dict[str, str]]:
    connection = store._require_connection()
    objects = connection.execute(
        "SELECT table_schema, table_name, table_type FROM information_schema.tables"
    ).fetchall()
    paper = [(s, n, t) for s, n, t in objects if str(n).casefold().startswith("paper_")]
    if {n for _, n, _ in paper} != set(TABLE_IDENTIFIERS) or any(
        s != "main" or t != "BASE TABLE" for s, _, t in paper
    ):
        raise EvidenceError("ledger_schema_unavailable")
    # Only metadata queries precede this check. An untrusted view must never be
    # executed even by the row-limit guard (views can read external files).
    store._validate_schema()
    # Bound before replay audit too; the legacy audit is intentionally unchanged.
    for table in TABLE_IDENTIFIERS:
        rows = connection.execute(f"SELECT 1 FROM {table} LIMIT {MAX_TABLE_ROWS + 1}").fetchall()
        if len(rows) > MAX_TABLE_ROWS:
            raise EvidenceError("ledger_row_limit")
    if store.audit_recovery_ledger()["status"] != "passed":
        raise EvidenceError("ledger_audit_unavailable")
    if not store.portfolio_exists(portfolio):
        raise EvidenceError("ledger_portfolio_missing")
    ledger: dict[str, Any] = {}
    run_accounts = {}
    for table, key in TABLE_IDENTIFIERS.items():
        cursor = connection.execute(
            f"SELECT * FROM {table} ORDER BY {key} LIMIT {MAX_TABLE_ROWS + 1}"
        )
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        if len(rows) > MAX_TABLE_ROWS:
            raise EvidenceError("ledger_row_limit")
        fingerprints = []
        for row in rows:
            original = dict(zip(columns, (_scalar(v) for v in row), strict=True))
            item = original[key]
            if key != "schema_version":
                identifier(item)
            fingerprints.append(
                {key: item, "row_sha256": hashlib.sha256(canonical_bytes(original)).hexdigest()}
            )
            if table == "paper_runs":
                run_accounts[item] = original["account_id"]
        ledger[table] = fingerprints
    return ledger, run_accounts


def _observe_root(root: Path, observer: Observation) -> None:
    for name in ("data", "outputs"):
        observer.listing(root / name, "directories")


def capture_run_acceptance(root: Path, portfolio_id: str = "default") -> dict[str, Any]:
    """Capture full ledger history and byte fingerprints without writing anything."""
    root, portfolio, root_id = _request(root, portfolio_id)
    observer = Observation(root)
    try:
        _observe_root(root, observer)
        archives = _archives(root, portfolio, observer)
        path = _database(root, observer)
        with PaperStore(path, read_only=True) as store:
            connection = store._require_connection()
            connection.execute("SET temp_directory = ''")
            connection.execute("BEGIN TRANSACTION")
            try:
                ledger, _ = _snapshot(store, portfolio)
            finally:
                connection.execute("ROLLBACK")
        value = {
            "schema_version": 1,
            "workflow": "paper_run_acceptance_baseline",
            "portfolio_id": portfolio,
            "root_id": root_id,
            "captured_at": _now(),
            "ledger": ledger,
            "archives": [
                {"archive_name": name, "sha256": hashlib.sha256(raw).hexdigest()}
                for name, raw in sorted(archives.items())
            ],
        }
        value["baseline_id"] = baseline_digest(value)
        validate_baseline(value, root_id, portfolio)
        if observer.changed():
            raise EvidenceError("input_changed")
        return value
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError("capture_unavailable") from exc


def _result(portfolio: str, baseline_id: str, started: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "paper_run_acceptance",
        "acceptance_scope": "recorded_automation_and_ledger_interval",
        "status": "attention",
        "portfolio_id": portfolio,
        "baseline_id": baseline_id,
        "recorded_attempt_count": None,
        "new_run_count": None,
        "checks": {
            "history_preserved": False,
            "archive_interval_complete": False,
            "exact_run_coverage": False,
            "all_attempts_successful": False,
            "repeat_noop_verified": False,
        },
        "attempts": [],
        "diagnostics": [],
        "observation": {
            "started_at": started,
            "completed_at": None,
            "consistency": "non_atomic",
            "input_changed": False,
        },
        "safety": dict(_SAFETY),
    }


def _history(
    baseline: dict[str, Any], current: dict[str, Any], run_accounts: dict[str, str]
) -> tuple[bool, set[str]]:
    before = baseline["ledger"]
    old_runs = {r["run_id"] for r in before["paper_runs"]}
    new_runs = set(run_accounts) - old_runs
    updated_accounts = {run_accounts[r] for r in new_runs}
    passed = all(
        before[table] == current[table] for table in ("paper_schema_metadata", "paper_accounts")
    )
    for table, key in (("paper_runs", "run_id"), ("paper_events", "event_id")):
        now = {r[key]: r for r in current[table]}
        passed &= all(now.get(r[key]) == r for r in before[table])
    states = {r["account_id"]: r for r in current["paper_state"]}
    passed &= set(states) == {r["account_id"] for r in current["paper_accounts"]}
    passed &= all(
        states.get(r["account_id"]) == r
        for r in before["paper_state"]
        if r["account_id"] not in updated_accounts
    )
    return passed, new_runs


def _interval(
    baseline: dict[str, Any],
    archives: dict[str, bytes],
    portfolio: str,
    attempt_ids: list[str],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    notes = result["diagnostics"]
    old = {r["archive_name"]: r["sha256"] for r in baseline["archives"]}
    preserved = all(
        name in archives and hashlib.sha256(archives[name]).hexdigest() == digest
        for name, digest in old.items()
    )
    if not preserved:
        notes.append(diagnostic("baseline_archive_changed", "archives"))
    records = []
    complete = preserved
    for name in sorted(archives.keys() - old.keys()):
        try:
            value = _validate(strict_json(archives[name]), portfolio, name)
            if value["schema_version"] != 2:
                raise ValueError("schema2_required")
            records.append(value)
        except (ValueError, TypeError, KeyError, RecursionError):
            complete = False
            notes.append(diagnostic("new_archive_invalid", "archives"))
    records.sort(key=lambda r: moment(r["started_at"]))
    times = [r["started_at"] for r in records]
    ids = [r["attempt_id"] for r in records]
    if len(set(times)) != len(times) or len(set(ids)) != len(ids):
        complete = False
        notes.append(diagnostic("archive_order_ambiguous", "archives"))
    if ids != attempt_ids:
        complete = False
        notes.append(diagnostic("attempt_selection_mismatch", "archives"))
    if any(moment(r["started_at"]) < moment(baseline["captured_at"]) for r in records):
        complete = False
        notes.append(diagnostic("archive_before_baseline", "archives"))
    result["checks"]["archive_interval_complete"] = complete
    result["recorded_attempt_count"] = len(records)
    return records


def verify_run_acceptance(
    root: Path, portfolio_id: str, baseline_path: Path, attempt_ids: Sequence[str]
) -> dict[str, Any]:
    """Verify every newly recorded attempt against the exact all-ledger interval."""
    started = _now()
    root, portfolio, root_id = _request(root, portfolio_id)
    if isinstance(attempt_ids, (str, bytes)) or not isinstance(attempt_ids, Sequence):
        raise PaperAcceptanceRequestError("attempt_ids_invalid")
    try:
        selected = [hex_digest(item, 32) for item in attempt_ids]
    except (ValueError, TypeError) as exc:
        raise PaperAcceptanceRequestError("attempt_ids_invalid") from exc
    if not isinstance(baseline_path, Path):
        raise PaperAcceptanceRequestError("baseline_path_invalid")
    baseline_path = Path(os.path.abspath(baseline_path))
    observer = Observation(root)
    raw = observer.read(baseline_path, "baseline", limit=MAX_BASELINE_BYTES)
    if raw is None:
        raise EvidenceError("baseline_missing")
    try:
        baseline = validate_baseline(strict_json(raw), root_id, portfolio)
    except (ValueError, TypeError, RecursionError) as exc:
        raise PaperAcceptanceRequestError("baseline_invalid") from exc
    result = _result(portfolio, baseline["baseline_id"], started)
    notes = result["diagnostics"]
    try:
        _observe_root(root, observer)
        archives = _archives(root, portfolio, observer, validate_names=False)
        records = _interval(baseline, archives, portfolio, selected, result)
        with PaperStore(_database(root, observer), read_only=True) as store:
            connection = store._require_connection()
            connection.execute("SET temp_directory = ''")
            connection.execute("BEGIN TRANSACTION")
            try:
                current, run_accounts = _snapshot(store, portfolio)
                history, new_runs = _history(baseline, current, run_accounts)
                result["checks"]["history_preserved"] = history
                result["new_run_count"] = len(new_runs)
                if not history:
                    notes.append(diagnostic("ledger_history_changed", "ledger"))
                old_runs = {r["run_id"] for r in baseline["ledger"]["paper_runs"]}
                claimed: set[str] = set()
                precise = True
                for value in records:
                    ledger = value["run_identity"]["ledger"]
                    correlation = store.correlate_run_references(portfolio, ledger)
                    refs = {r["run_id"] for r in ledger["runs"]}
                    if refs & old_runs:
                        notes.append(diagnostic("baseline_run_reused", "ledger"))
                        precise = False
                    if refs & claimed:
                        notes.append(diagnostic("run_claimed_twice", "ledger"))
                        precise = False
                    if correlation["status"] not in {
                        "verified",
                        "partial_verified",
                        "no_new_commits",
                    }:
                        notes.append(diagnostic("run_correlation_not_verified", "ledger"))
                        precise = False
                    claimed |= refs
                    result["attempts"].append({**_safe_record(value), "correlation": correlation})
                if new_runs - claimed:
                    notes.append(diagnostic("unclaimed_commits", "ledger"))
                if claimed != new_runs:
                    precise = False
                    notes.append(diagnostic("run_coverage_mismatch", "ledger"))
                result["checks"]["exact_run_coverage"] = precise
            finally:
                connection.execute("ROLLBACK")
        completed = _now()
        if any(moment(r["completed_at"]) > moment(completed) for r in records):
            result["checks"]["archive_interval_complete"] = False
            notes.append(diagnostic("archive_after_observation", "archives"))
        successful = bool(records) and all(r["status"] == "success" for r in records)
        result["checks"]["all_attempts_successful"] = successful
        if not successful:
            notes.append(diagnostic("attempts_not_successful", "archives"))
        repeat = (
            bool(records)
            and records[-1]["operation_status"] == "no-op"
            and (records[-1]["run_identity"]["ledger"] == {"scope": "complete", "runs": []})
            and any(
                r["status"] == "success"
                and r["operation_status"] == "processed"
                and r["as_of_date"] == records[-1]["as_of_date"]
                for r in records[:-1]
            )
        )
        repeat = repeat and all(
            result["checks"][key] for key in result["checks"] if key != "repeat_noop_verified"
        )
        result["checks"]["repeat_noop_verified"] = repeat
        if not repeat:
            notes.append(diagnostic("repeat_noop_not_verified", "archives"))
        result["status"] = "passed" if all(result["checks"].values()) else "attention"
    except Exception:
        result["status"] = "unavailable"
        notes.append(diagnostic("evidence_unavailable", "observation"))
    changed = observer.changed()
    if changed:
        result["status"] = "unavailable"
        result["checks"]["repeat_noop_verified"] = False
        notes.append(diagnostic("input_changed", "observation"))
    result["observation"].update(completed_at=_now(), input_changed=bool(changed))
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise PaperAcceptanceRequestError("arguments_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", type=Path, required=True)
        sub.add_argument("--portfolio", default="default")
        if name == "verify":
            sub.add_argument("--baseline", type=Path, required=True)
            sub.add_argument("--attempt-id", action="append", default=[])
    try:
        args = parser.parse_args(argv)
        if args.command == "capture":
            result = capture_run_acceptance(args.root, args.portfolio)
            code = 0
        else:
            result = verify_run_acceptance(
                args.root, args.portfolio, args.baseline, args.attempt_id
            )
            code = 0 if result["status"] == "passed" else 1
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except PaperAcceptanceRequestError as exc:
        encoded = json.dumps({"status": "error", "diagnostics": [diagnostic(str(exc), "request")]})
        code = 2
    except Exception:
        encoded = json.dumps(
            {
                "status": "unavailable",
                "diagnostics": [diagnostic("evidence_unavailable", "observation")],
            }
        )
        code = 1
    print(encoded)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
