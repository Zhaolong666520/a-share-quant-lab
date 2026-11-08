"""Explicit schedule assessment and bounded JSON evidence, without database access."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from finance_lab.operational_status_io import Observation, strict_json
from finance_lab.paper_run_identity import parse_references, validate_health, validate_report
from finance_lab.paper_runtime_evidence import day, moment

_SHANGHAI = timezone(timedelta(hours=8))


def assess_start(
    scheduler: dict[str, Any], expected_start: datetime | None, now: datetime
) -> dict[str, Any]:
    """Describe the latest system start; never infer a historical trigger or attempt identity."""
    result: dict[str, Any] = {
        "expected_start_at": expected_start.isoformat() if expected_start else None,
        "expectation_origin": "explicit_user_time" if expected_start else "not_provided",
        "system_last_start_at": scheduler.get("last_run_time"),
        "state": "unknown",
        "reason": "expected_time_not_provided",
        "delay_seconds": None,
        "scheduler_application_correlation": "not_verified",
    }
    if expected_start is None:
        return result
    if now < expected_start:
        return {**result, "state": "not_due", "reason": "before_expected_time"}
    if scheduler.get("status") != "available":
        return {**result, "reason": "scheduler_unavailable"}
    stamp = scheduler.get("last_run_time")
    if stamp is None:
        return {**result, "state": "start_not_observed", "reason": "no_last_start"}
    started = moment(stamp)
    expected_day = expected_start.astimezone(_SHANGHAI).date()
    actual_day = started.astimezone(_SHANGHAI).date()
    if actual_day < expected_day:
        return {**result, "state": "start_not_observed", "reason": "older_last_start"}
    if actual_day > expected_day:
        return {**result, "reason": "latest_start_is_later_date"}
    if started < expected_start:
        return {**result, "state": "start_not_observed", "reason": "only_earlier_start"}
    if scheduler.get("trigger_origin") != "time_trigger":
        return {**result, "reason": "time_trigger_not_verified"}
    delay = (started - expected_start).total_seconds()
    return {
        **result,
        "state": "delayed" if delay > 0 else "on_time",
        "reason": "explicit_time_comparison",
        "delay_seconds": delay,
    }


def unknown_report(reason: str, *, state: str = "unknown") -> dict[str, Any]:
    """Clear all derived values when any binding input becomes unusable."""
    return {
        "state": state,
        "reason": reason,
        "scope": "latest_archive_json_only",
        "sha256_matched": False,
        "content_identity_matched": False,
        "receipt_status": "unknown",
        "checked_receipts": 0,
        "unchecked_earlier_receipts": 0,
        "data_end_date": None,
        "data_date_origin": "sha256_bound_report_declaration",
        "database_events_verified": False,
        "other_report_artifacts_verified": False,
    }


def inspect_report_json(
    root: Path, portfolio: str, latest: dict[str, Any] | None, observer: Observation
) -> dict[str, Any]:
    """Verify an archive-bound JSON and its last-day receipts; do not audit the database."""
    if latest is None or latest.get("status") != "success":
        return unknown_report("no_successful_latest_attempt", state="not_applicable")
    try:
        identity = latest.get("run_identity")
        if not isinstance(identity, dict):
            return unknown_report("archive_identity_not_recorded")
        report_ref = validate_report(identity.get("report"))
        health = validate_health(identity.get("health"))
        if report_ref is None or health is None:
            return unknown_report("archive_identity_not_recorded")
        raw = observer.read(root / "outputs" / report_ref["archive_json_name"], "runtime_report")
        if raw is None:
            return unknown_report("report_missing", state="missing")
        if hashlib.sha256(raw).hexdigest() != report_ref["archive_json_sha256"]:
            return unknown_report("report_sha256_mismatch", state="mismatch")
        report = strict_json(raw)
        if not isinstance(report, dict):
            raise ValueError("report_structure")
        processed = latest["processed_dates"]
        if not isinstance(processed, list) or processed != sorted(set(processed)):
            raise ValueError("processed_dates")
        for value in processed:
            day(value)
        context = report["data_context"]
        if (
            report["portfolio_id"] != portfolio
            or report["operation_status"] != latest["operation_status"]
            or report["processed_dates"] != processed
            or context["dataset_id"] != health["dataset_id"]
            or context["as_of_date"] != health["as_of_date"]
        ):
            raise ValueError("report_identity")
        end = day(context["data_end_date"])
        if end > day(health["as_of_date"]):
            raise ValueError("future_data_date")
        accounts = report["accounts"]
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("accounts")
        account_ids = [a["account_id"] for a in accounts]
        if any(not isinstance(v, str) or not v for v in account_ids):
            raise ValueError("account_id")
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("duplicate_account")
        for account in accounts:
            state, run = account["state"], account["latest_run"]
            run_health = run["data_health"]
            valuation = day(state["last_trade_date"])
            run_end = day(run_health["data_end_date"])
            run_as_of = day(run_health["as_of_date"])
            if (
                state["account_id"] != account["account_id"]
                or valuation != day(run["trade_date"])
                or not valuation <= run_end <= end
                or not run_end <= run_as_of <= day(health["as_of_date"])
                or run_health["dataset_id"] != run["dataset_id"]
                or run_health["curated_sha256"] != run["curated_sha256"]
            ):
                raise ValueError("account_internal_identity")
        ledger = identity["ledger"]
        references = parse_references(ledger["runs"])
        if ledger["scope"] != "complete":
            return unknown_report("receipt_scope_unknown")
        if sorted({r.trade_date.isoformat() for r in references}) != processed:
            raise ValueError("receipt_dates")
        if not processed:
            if latest["operation_status"] != "no-op" or references:
                raise ValueError("empty_processed_dates")
            return {
                **unknown_report("json_identity_verified"),
                "state": "matched",
                "sha256_matched": True,
                "content_identity_matched": True,
                "receipt_status": "not_applicable_no_new_receipts",
                "data_end_date": end.isoformat(),
            }
        final_day = day(processed[-1])
        if final_day > end:
            raise ValueError("processed_after_data")
        final_refs = {r.account_id: r for r in references if r.trade_date == final_day}
        if set(final_refs) != set(account_ids):
            raise ValueError("receipt_accounts")
        for account in accounts:
            ref = final_refs[account["account_id"]]
            state, run = account["state"], account["latest_run"]
            if (
                state["account_id"] != ref.account_id
                or state["last_trade_date"] != final_day.isoformat()
                or run["trade_date"] != final_day.isoformat()
            ):
                raise ValueError("account_date")
            for field in ("run_id", "batch_id", "dataset_id", "curated_sha256", "state_hash"):
                if run[field] != getattr(ref, field):
                    raise ValueError("receipt_identity")
            run_health = run["data_health"]
            if (
                run["dataset_id"] != context["dataset_id"]
                or run["curated_sha256"] != context["curated_sha256"]
                or run_health["dataset_id"] != context["dataset_id"]
                or run_health["curated_sha256"] != context["curated_sha256"]
                or run_health["as_of_date"] != context["as_of_date"]
                or day(run_health["data_end_date"]) != end
            ):
                raise ValueError("account_data_context")
        return {
            **unknown_report("json_identity_and_final_receipts_verified"),
            "state": "matched",
            "sha256_matched": True,
            "content_identity_matched": True,
            "receipt_status": "last_processed_date_matched",
            "checked_receipts": len(final_refs),
            "unchecked_earlier_receipts": len(references) - len(final_refs),
            "data_end_date": end.isoformat(),
        }
    except (KeyError, TypeError, ValueError, OSError, AttributeError, OverflowError):
        return unknown_report("report_unreadable_or_inconsistent")
