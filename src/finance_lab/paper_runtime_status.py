"""Read-only daily runtime diagnosis; expected completed trading date must be explicit."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from finance_lab.operational_status_io import Observation, diagnostic, validate_root
from finance_lab.paper_models import validate_portfolio_id
from finance_lab.paper_operational_status import (
    _invalidate_recorded_observation,
    inspect_paper_operational_status,
)
from finance_lab.paper_process_status import inspect_process_evidence
from finance_lab.paper_runtime_assessment import assess_start, inspect_report_json, unknown_report
from finance_lab.paper_runtime_evidence import (
    RuntimeObservation,
    day,
    moment,
    read_runtime_evidence,
    read_scheduler_status,
)

_SHANGHAI = timezone(timedelta(hours=8))
_DEFAULT_TASK = "A-Share Quant Lab Paper Daily"
# Documented SCHED_S_* statuses that do not establish an action's completion.
# https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-error-and-success-constants
_NON_COMPLETION_RESULTS = {
    0x41300,
    0x41302,
    0x41303,
    0x41304,
    0x41305,
    0x41307,
    0x41308,
    0x4131B,
    0x4131C,
}
_SAFETY = {
    "broker_connected": False,
    "authorizes_real_money": False,
    "current_health_verified": False,
    "report_artifacts_verified": False,
    "process_liveness_verified": False,
    "scheduler_application_correlation": "not_verified",
    "natural_run_acceptance_verified": False,
}


def _scheduler(raw: dict[str, Any], name: str, now: datetime) -> dict[str, Any]:
    unknown = {"status": "unknown", "completion_state": "unknown", "trigger_origin": "unknown"}
    try:
        if raw.get("status") != "available":
            return unknown
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or raw["task_name"] != name
            or raw["state"] not in {"Ready", "Running", "Queued", "Disabled", "Unknown"}
        ):
            raise ValueError("scheduler_protocol")
        age = (now - moment(raw["observed_at"])).total_seconds()
        if not 0 <= age <= 60:
            raise ValueError("snapshot_age")
        for key in (
            "enabled",
            "start_when_available",
            "disallow_start_on_batteries",
            "stop_on_batteries",
            "wake_to_run",
        ):
            if type(raw[key]) is not bool:
                raise ValueError("scheduler_bool")
        for key in ("last_task_result", "missed_runs"):
            if type(raw[key]) is not int or not 0 <= raw[key] <= 0xFFFFFFFF:
                raise ValueError("scheduler_result")
        for key in ("last_run_time", "next_run_time"):
            if raw[key] is not None:
                stamp = moment(raw[key])
                if key == "last_run_time" and stamp > now:
                    raise ValueError("scheduler_future")
        if raw["trigger_origin"] not in {"time_trigger", "manual", "unknown"}:
            raise ValueError("scheduler_origin")
        active = raw["state"] in {"Running", "Queued"} or raw["last_task_result"] in {
            0x41301,
            0x41325,
        }
        completion = (
            "not_completed"
            if active
            else "unknown"
            if raw["last_run_time"] is None or raw["last_task_result"] in _NON_COMPLETION_RESULTS
            else "succeeded"
            if raw["last_task_result"] == 0
            else "failed"
        )
        keys = (
            "task_name",
            "observed_at",
            "state",
            "enabled",
            "last_run_time",
            "last_task_result",
            "next_run_time",
            "missed_runs",
            "start_when_available",
            "disallow_start_on_batteries",
            "stop_on_batteries",
            "wake_to_run",
            "trigger_origin",
        )
        return {
            **{key: raw[key] for key in keys},
            "status": "available",
            "completion_state": completion,
            "result_origin": "scheduler_snapshot",
        }
    except (KeyError, ValueError, TypeError, OverflowError):
        return unknown


def _success_day(attempt: dict[str, Any] | None) -> str | None:
    try:
        value = attempt["run_identity"]["health"]["as_of_date"]  # type: ignore[index]
        return day(value).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def _error(code: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": "paper_runtime_status",
        "status": "error",
        "command_exit_code": 2,
        "safety": _SAFETY,
        "diagnostics": [diagnostic(code, "request")],
    }


def inspect_paper_runtime_status(
    root: Path,
    portfolio_id: str,
    expected_as_of: str,
    *,
    now: datetime | None = None,
    task_name: str = _DEFAULT_TASK,
    query_scheduler: bool = True,
    expected_start_at: str | None = None,
) -> dict[str, Any]:
    fixed_clock = now is not None
    now = datetime.now(UTC) if now is None else now
    try:
        if now.utcoffset() is None:
            raise ValueError("timezone")
        expected = day(expected_as_of)
        expected_start = moment(expected_start_at) if expected_start_at is not None else None
        if expected_start is not None and expected_start.astimezone(_SHANGHAI).date() != expected:
            raise ValueError("scheduled_date_mismatch")
        if expected > now.astimezone(_SHANGHAI).date():
            raise ValueError("future_expected_date")
        root = validate_root(root)
        portfolio = validate_portfolio_id(portfolio_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,127}", task_name):
            raise ValueError("task_name")
    except (OSError, ValueError, TypeError, AttributeError):
        return _error("runtime_request_invalid")

    observer = RuntimeObservation(root)
    runtime = read_runtime_evidence(root, portfolio, observer)
    # Keep the runtime reader's earlier marker signature independent. Re-reading
    # that path in the recorded reader must not overwrite the earlier capture.
    recorded_observer = Observation(root)
    recorded = inspect_paper_operational_status(root, portfolio, _observer=recorded_observer)
    if recorded["command_exit_code"] == 2:
        return _error("recorded_status_unavailable")
    report_json = inspect_report_json(
        root, portfolio, recorded["automation"]["latest_attempt"], observer
    )
    raw_scheduler = read_scheduler_status(task_name) if query_scheduler else {}
    scheduler_now = now if fixed_clock else datetime.now(UTC)
    scheduler = (
        _scheduler(raw_scheduler, task_name, scheduler_now)
        if query_scheduler
        else {"status": "unknown", "completion_state": "unknown", "trigger_origin": "unknown"}
    )
    now = scheduler_now
    process_evidence = inspect_process_evidence(root, portfolio, now=now)
    changed = observer.changed() | recorded_observer.changed()
    if "runtime_marker" in changed:
        changed.add("market")
    recorded = _invalidate_recorded_observation(recorded, changed)
    notes = list(recorded["diagnostics"])
    if changed & {"automation", "runtime_report"}:
        report_json = unknown_report("report_binding_input_changed", state="changed")
    if "market" in changed:
        runtime["marker"] = {"state": "changed"}
    for section, key in (("runtime_log", "log"), ("runtime_marker", "marker")):
        if section in changed:
            runtime[key] = {"state": "changed"}
            notes.append(diagnostic("runtime_input_changed", section))
    log, marker = runtime["log"], runtime["marker"]
    try:
        times = [
            moment(r[k])
            for r, k in (
                (log.get("latest_start"), "started_at"),
                (log.get("latest_terminal"), "recorded_at"),
            )
            if r is not None
        ]
        start = log.get("latest_start")
        future_request = (
            start is not None and day(start["as_of_date"]) > now.astimezone(_SHANGHAI).date()
        )
        if any(t > now for t in times) or future_request:
            log = {"state": "unavailable"}
            notes.append(diagnostic("runtime_evidence_future", "runtime_log"))
        if marker["state"] == "available":
            start = moment(marker["started_at"])
            end = moment(marker["completed_at"]) if marker["completed_at"] else now
            if start > now or end > now or day(marker["end"]) > now.astimezone(_SHANGHAI).date():
                marker = {"state": "unavailable"}
                notes.append(diagnostic("runtime_evidence_future", "runtime_marker"))
            else:
                marker["elapsed_seconds"] = (end - start).total_seconds()
    except (ValueError, TypeError, KeyError, OverflowError):
        log, marker = {"state": "unavailable"}, {"state": "unavailable"}

    automation = recorded["automation"]
    latest, success = automation["latest_attempt"], automation["last_success"]
    if any(
        r is not None and (moment(r["started_at"]) > now or moment(r["completed_at"]) > now)
        for r in (latest, success)
    ):
        notes.append(diagnostic("archive_timestamp_future", "application"))
        if latest and (moment(latest["started_at"]) > now or moment(latest["completed_at"]) > now):
            latest = None
            report_json = unknown_report("archive_timestamp_future")
        if success and (
            moment(success["started_at"]) > now or moment(success["completed_at"]) > now
        ):
            success = None
    unarchived = "unknown"
    for label, attempt in (("latest", latest), ("success", success)):
        requested = _success_day(attempt)
        if requested is not None and day(requested) > now.astimezone(_SHANGHAI).date():
            notes.append(diagnostic("archive_request_date_future", "application"))
            if label == "latest":
                latest = None
                report_json = unknown_report("archive_request_date_future")
            else:
                success = None
    if "automation" in changed:
        notes.append(diagnostic("application_evidence_changed", "application"))
    elif log["state"] == "available" and log.get("latest_start"):
        started = moment(log["latest_start"]["started_at"])
        unarchived = (
            "observed"
            if latest is None or started > moment(latest["completed_at"])
            else ("not_observed")
        )
        if unarchived == "observed":
            notes.append(diagnostic("application_start_without_archive", "application"))
    else:
        notes.append(diagnostic("runtime_log_unavailable", "application"))
    terminal = log.get("latest_terminal")
    if (
        terminal
        and "automation" not in changed
        and terminal["status"] == "failed"
        and (latest is None or moment(terminal["recorded_at"]) > moment(latest["completed_at"]))
    ):
        notes.append(diagnostic("application_log_failure_newer_than_archive", "application"))
    if marker["state"] != "available":
        notes.append(diagnostic("runtime_marker_unavailable", "market"))
    elif marker["status"] != "completed":
        notes.append(diagnostic("market_update_incomplete", "market"))

    success_date = _success_day(success)
    schedule = assess_start(scheduler, expected_start, now)
    not_due = schedule["state"] == "not_due"
    if expected_start is not None and schedule["state"] not in {"on_time", "not_due"}:
        code = {
            "delayed": "scheduler_start_delayed",
            "start_not_observed": "expected_start_not_observed",
        }.get(schedule["state"], "scheduler_start_time_unknown")
        notes.append(diagnostic(code, "schedule"))
    if latest is not None and latest["status"] == "success" and report_json["state"] != "matched":
        notes.append(diagnostic("report_json_not_verified", "report"))
    valuation_date = recorded["ledger"]["last_valuation_date"]
    progress: dict[str, Any] = {
        "expected_as_of": expected_as_of,
        "expectation_origin": "explicit_user_date",
        "last_success_as_of": success_date,
        "last_valuation_date": valuation_date,
    }
    for key, value in (("application", success_date), ("valuation", valuation_date)):
        try:
            observed = day(value)
            if observed > now.astimezone(_SHANGHAI).date():
                raise ValueError("future_date")
            progress[key] = "met" if observed >= expected else "behind"
        except (ValueError, TypeError):
            progress[key] = "unknown"
        if not_due and progress[key] == "behind":
            progress[key] = "not_due"
        elif progress[key] != "met":
            suffix = (
                "behind_expected_date" if progress[key] == "behind" else "expected_date_unknown"
            )
            notes.append(diagnostic(key + "_" + suffix, "progress"))
    if scheduler["status"] == "unknown":
        notes.append(diagnostic("scheduler_unavailable", "scheduler"))
    else:
        if not not_due and scheduler["last_run_time"] is not None and (
            moment(scheduler["last_run_time"]).astimezone(_SHANGHAI).date() < expected
        ):
            notes.append(diagnostic("scheduler_behind_expected_date", "scheduler"))
        if not scheduler["enabled"] or scheduler["state"] in {"Disabled", "Unknown"}:
            notes.append(diagnostic("scheduler_not_ready", "scheduler"))
        if scheduler["completion_state"] != "succeeded":
            code = (
                "scheduler_last_run_failed"
                if scheduler["completion_state"] == "failed"
                else ("scheduler_completion_not_confirmed")
            )
            notes.append(diagnostic(code, "scheduler"))
        if scheduler["missed_runs"]:
            notes.append(diagnostic("scheduler_recorded_missed_runs", "scheduler"))
        if scheduler["trigger_origin"] == "unknown":
            notes.append(diagnostic("scheduler_trigger_origin_unknown", "scheduler"))
    if process_evidence["state"] not in {"missing", "completed"}:
        notes.append(diagnostic("process_" + process_evidence["state"], "process"))
    attention = bool(notes) or recorded["status"] != "ok"
    return {
        "schema_version": 1,
        "workflow": "paper_runtime_status",
        "status": "attention" if attention else "ok",
        "command_exit_code": int(attention),
        "observation": {
            "root": str(root),
            "portfolio_id": portfolio,
            "observed_at": now.isoformat(),
            "consistency": "non_atomic",
            "runtime_input_changed": bool(changed),
            "changed_sections": sorted(changed),
        },
        "safety": _SAFETY,
        "progress": progress,
        "scheduler": scheduler,
        "schedule": schedule,
        "report_json": report_json,
        "process_evidence": process_evidence,
        "application": {
            "latest_archived_attempt": latest,
            "last_success": success,
            "log": log,
            "unarchived_start": unarchived,
            "log_archive_association": "temporal_observation_only",
        },
        "market_update": {**marker, "process_state": "unknown"},
        "recorded_status": recorded,
        "diagnostics": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--portfolio", required=True)
    parser.add_argument("--expected-as-of", required=True)
    parser.add_argument("--expected-start-at", help="Explicit scheduled timestamp with timezone")
    parser.add_argument("--task-name", default=_DEFAULT_TASK)
    parser.add_argument("--no-scheduler", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = inspect_paper_runtime_status(
            args.root,
            args.portfolio,
            args.expected_as_of,
            task_name=args.task_name,
            query_scheduler=not args.no_scheduler,
            expected_start_at=args.expected_start_at,
        )
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except Exception:
        result = _error("runtime_status_unavailable")
        encoded = json.dumps(result)
    print(encoded)
    return int(result["command_exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
