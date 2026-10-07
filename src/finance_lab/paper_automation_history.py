"""Read schema-1/2 daily automation archives without importing their writer."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from finance_lab.operational_status_io import (
    EvidenceError,
    Observation,
    diagnostic,
    strict_json,
    validate_root,
)
from finance_lab.paper_models import validate_portfolio_id
from finance_lab.paper_run_identity import validate_run_identity

_OPTIONAL = {"failed_sources", "market_data_committed", "ledger_committed", "next_action"}
_REQUIRED = {
    "schema_version",
    "workflow",
    "status",
    "portfolio_id",
    "start_date",
    "as_of_date",
    "stale_after_business_days",
    "started_at",
    "completed_at",
    "steps",
    "failed_step",
    "error",
    "successful_instruments",
    "data_health_status",
    "data_health_report_path",
    "operation_status",
    "processed_dates",
    "paper_report_path",
    "broker_connected",
    "authorizes_real_money",
    "run_id",
}


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date")
    return parsed


def _moment(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo != UTC or parsed.isoformat().replace("+00:00", "Z") != value:
        raise ValueError("timestamp")
    return parsed


def _validate(value: Any, portfolio: str, filename: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or not _REQUIRED <= value.keys():
        raise ValueError("fields")
    version = value["schema_version"]
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("version")
    identity_fields = {"attempt_id", "run_identity"} if version == 2 else set()
    required = _REQUIRED | identity_fields | (_OPTIONAL if version == 2 else set())
    if not required <= value.keys() or value.keys() - (_REQUIRED | _OPTIONAL | identity_fields):
        raise ValueError("fields")
    original = {k: v for k, v in value.items() if k != "run_id"}
    digest = hashlib.sha256(
        json.dumps(
            original, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("ascii")
    )
    if not isinstance(value["run_id"], str) or not re.fullmatch(r"[0-9a-f]{16}", value["run_id"]):
        raise ValueError("run_id")
    if value["run_id"] != digest.hexdigest()[:16]:
        raise ValueError("hash")
    if value["workflow"] != "paper_daily" or value["portfolio_id"] != portfolio:
        raise ValueError("identity")
    if value["broker_connected"] is not False or value["authorizes_real_money"] is not False:
        raise ValueError("safety")
    start, end = _day(value["start_date"]), _day(value["as_of_date"])
    begun, ended = _moment(value["started_at"]), _moment(value["completed_at"])
    if start >= end or begun > ended:
        raise ValueError("range")
    expected = f"{portfolio}_{begun.strftime('%Y%m%dT%H%M%S%fZ')}_{value['run_id']}.json"
    if filename is not None and filename != expected:
        raise ValueError("filename")
    for key in ("stale_after_business_days", "successful_instruments", "failed_sources"):
        item = value.get(key)
        if item is None and key != "stale_after_business_days":
            continue
        if type(item) is not int or item < 0:
            raise ValueError("count")
    for key in ("market_data_committed", "ledger_committed"):
        if value.get(key) is not None and type(value[key]) is not bool:
            raise ValueError("commit")
    for key in ("paper_report_path", "data_health_report_path", "next_action"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise ValueError("metadata")
    if value["data_health_status"] not in {None, "pass", "warning", "error"}:
        raise ValueError("health")
    steps = value["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps")
    normalized: list[tuple[str, str]] = []
    for item in steps:
        if not isinstance(item, dict) or set(item) != {"name", "status"}:
            raise ValueError("step")
        if not isinstance(item["name"], str) or not isinstance(item["status"], str):
            raise ValueError("step")
        normalized.append((item["name"], item["status"]))
    run = [("market_data_update", "succeeded"), ("data_health", "succeeded")]
    branches = [
        run + [("paper_run", "succeeded")],
        run + [("paper_run", "portfolio_missing"), ("paper_init", "succeeded")],
    ]
    status = value["status"]
    if status == "success":
        if (
            normalized not in branches
            or value["error"] is not None
            or value["failed_step"] is not None
        ):
            raise ValueError("success")
    elif status == "failed":
        failed_step = value["failed_step"]
        failures = [
            [("market_data_update", "failed")],
            [run[0], ("data_health", "failed")],
            run + [("paper_run", "failed")],
            run + [("paper_run", "portfolio_missing"), ("paper_init", "failed")],
            *[b + [("paper_report", "failed")] for b in branches],
        ]
        error = value["error"]
        if normalized not in failures or failed_step != normalized[-1][0]:
            raise ValueError("failure")
        if not isinstance(error, dict) or not {"type", "message"} <= error.keys():
            raise ValueError("error")
        if error.keys() - {"type", "message", "code"} or not all(
            isinstance(v, str) for v in error.values()
        ):
            raise ValueError("error")
        if "code" in error and error["code"] != f"{failed_step}_failed":
            raise ValueError("error_code")
    else:
        raise ValueError("status")
    operation = value["operation_status"]
    dates = value["processed_dates"]
    if not isinstance(dates, list):
        raise ValueError("dates")
    parsed = [_day(item) for item in dates]
    if any(d > end for d in parsed) or any(
        a >= b for a, b in zip(parsed, parsed[1:], strict=False)
    ):
        raise ValueError("dates")
    returned = status == "success" or value["failed_step"] == "paper_report"
    if returned:
        op_step = normalized[-1 if status == "success" else -2][0]
        valid = (op_step == "paper_init" and operation == "initialized" and len(dates) == 1) or (
            op_step == "paper_run"
            and ((operation == "processed" and bool(dates)) or (operation == "no-op" and not dates))
        )
        if not valid or value["data_health_status"] not in {"pass", "warning"}:
            raise ValueError("operation")
        if value["successful_instruments"] is None or value["successful_instruments"] <= 0:
            raise ValueError("update_success")
        if "failed_sources" in value and value["failed_sources"] != 0:
            raise ValueError("failed_sources")
        if "ledger_committed" in value and value["ledger_committed"] is not bool(dates):
            raise ValueError("ledger_commit")
    elif version == 2 and operation == "partial":
        if (
            value["failed_step"] != "paper_run"
            or not dates
            or value.get("ledger_committed") is not True
        ):
            raise ValueError("partial_operation")
    elif operation is not None or dates or value.get("ledger_committed") is not None:
        raise ValueError("unreturned_operation")
    if len(normalized) > 1:
        if value["successful_instruments"] is None or value["successful_instruments"] <= 0:
            raise ValueError("update_success")
        if "market_data_committed" in value and value["market_data_committed"] is not True:
            raise ValueError("market_commit")
        if "failed_sources" in value and value["failed_sources"] != 0:
            raise ValueError("update_failure")
    if len(normalized) > 2 and value["data_health_status"] not in {"pass", "warning"}:
        raise ValueError("health_precondition")
    successful = value["successful_instruments"]
    committed = value.get("market_data_committed")
    if committed is not None and (successful is None or committed is not (successful > 0)):
        raise ValueError("market_commit_count")
    if version == 2:
        validate_run_identity(value)
    return value


def _safe_record(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        k: value.get(k)
        for k in (
            "run_id",
            "started_at",
            "completed_at",
            "status",
            "operation_status",
            "processed_dates",
            "failed_step",
            "successful_instruments",
            "failed_sources",
            "market_data_committed",
            "ledger_committed",
            "data_health_status",
        )
    }
    result.update(
        schema_version=value["schema_version"],
        attempt_id=value.get("attempt_id"),
        run_identity=value.get("run_identity"),
        state="failed_after_commit"
        if value["status"] == "failed" and value.get("ledger_committed") is True
        else value["status"],
        run_id_kind="automation_content_hash",
        exit_code=0 if value["status"] == "success" else 1,
        exit_code_origin="derived",
        report_path_recorded=bool(value["paper_report_path"]),
        required_fields_known=all(k in value for k in _OPTIONAL)
        and (value["error"] is None or "code" in value["error"]),
        error_code=None if value["error"] is None else value["error"].get("code"),
        error_code_origin="recorded"
        if value["error"] is not None and "code" in value["error"]
        else "unknown",
    )
    return result


def _inspect_history(root: Path, portfolio: str, observer: Observation) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "missing",
        "latest_attempt": None,
        "last_success": None,
        "latest_candidates": [],
        "last_success_candidates": [],
        "latest_mirror": {"state": "missing"},
        "scan": {"complete": True, "visible_archive_count": 0, "coverage": "this_directory_only"},
        "diagnostics": [],
    }
    notes = result["diagnostics"]
    directory = root / "outputs" / "paper_automation_runs"
    records: list[tuple[datetime, dict[str, Any] | None]] = []
    unordered = False
    try:
        candidates = observer.listing(directory, "automation", f"{portfolio}_")
    except (OSError, EvidenceError):
        candidates = None
        unordered = True
        notes.append(diagnostic("archive_scan_unavailable", "automation"))
    if candidates is not None:
        result["state"] = "empty" if not candidates else "available"
        result["scan"]["visible_archive_count"] = len(candidates)
    for path in candidates or []:
        match = re.fullmatch(
            re.escape(portfolio) + r"_(\d{8}T\d{12}Z)_([0-9a-f]{16})\.json", path.name
        )
        try:
            if match is None:
                raise ValueError("name")
            moment = datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
        except ValueError:
            unordered = True
            notes.append(diagnostic("archive_order_unknown", "automation"))
            continue
        try:
            raw = observer.read(path, "automation")
            value = _validate(strict_json(raw or b""), portfolio, path.name)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            value = None
            notes.append(diagnostic("archive_invalid", "automation"))
        records.append((moment, value))
    valid = [(t, v) for t, v in records if v is not None]
    successes = [(t, v) for t, v in valid if v["status"] == "success"]
    latest_raw: dict[str, Any] | None = None
    for source, field, candidate_field in (
        (records, "latest_attempt", "latest_candidates"),
        (successes, "last_success", "last_success_candidates"),
    ):
        if not source:
            continue
        latest_time = max(t for t, _ in source)
        peers = [v for t, v in source if t == latest_time]
        result[candidate_field] = [_safe_record(v) for v in peers if v is not None]
        if (
            len(peers) == 1
            and peers[0] is not None
            and not (unordered and field == "latest_attempt")
        ):
            result[field] = _safe_record(peers[0])
            if field == "latest_attempt":
                latest_raw = peers[0]
        elif field == "latest_attempt":
            result["state"] = "ambiguous" if len(peers) > 1 else "unknown"
            notes.append(diagnostic("latest_attempt_unknown", "automation"))
        else:
            notes.append(diagnostic("last_success_ambiguous", "automation"))
    if unordered:
        result["state"] = "unknown"
        result["latest_attempt"] = None
    result["scan"]["complete"] = not unordered and all(v is not None for _, v in records)
    mirror_path = root / "outputs" / f"{portfolio}_paper_automation_latest.json"
    try:
        raw = observer.read(mirror_path, "automation")
        if raw is not None:
            mirror = _validate(strict_json(raw), portfolio)
            matches = [v for _, v in valid if v == mirror]
            state = "orphan" if not matches else ("matched" if latest_raw == mirror else "behind")
            result["latest_mirror"] = {"state": state}
            if state != "matched":
                notes.append(diagnostic(f"latest_mirror_{state}", "automation"))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        result["latest_mirror"] = {"state": "invalid"}
        notes.append(diagnostic("latest_mirror_invalid", "automation"))
    if result["latest_mirror"]["state"] == "missing":
        notes.append(diagnostic("latest_mirror_missing", "automation"))
    if result["latest_attempt"] is None:
        notes.append(diagnostic("automation_unavailable", "automation"))
    if result["last_success"] is None:
        notes.append(diagnostic("no_visible_valid_success", "automation"))
    return result


def inspect_automation_history(root: Path, portfolio_id: str) -> dict[str, Any]:
    root = validate_root(root)
    portfolio = validate_portfolio_id(portfolio_id)
    observer = Observation(root)
    result = _inspect_history(root, portfolio, observer)
    if observer.changed():
        result.update(state="changed", latest_attempt=None, last_success=None)
        result["scan"]["complete"] = False
        result["diagnostics"].append(diagnostic("input_changed", "automation"))
    return result
