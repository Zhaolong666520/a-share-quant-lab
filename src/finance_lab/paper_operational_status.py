"""Independent read-only operational evidence command, not a recovery authorization."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from finance_lab.market_data_state import parse_market_update_state
from finance_lab.operational_status_io import (
    Observation,
    diagnostic,
    safe_path,
    strict_json,
    validate_root,
)
from finance_lab.paper_automation_history import _inspect_history
from finance_lab.paper_models import validate_portfolio_id
from finance_lab.paper_store import PaperStore

_SAFETY = {
    "broker_connected": False,
    "authorizes_real_money": False,
    "current_health_verified": False,
    "scheduler_status": "unknown",
    "automation_ledger_correlation": "not_available",
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _request_window_context(record: dict[str, Any]) -> dict[str, Any]:
    unknown = {
        "state": "unknown", "primary": None, "reference": None, "relation": "unknown",
        "explanation": "请求范围缺失或无法可靠解释；窗口内逐日覆盖未知。",
    }
    primary_source = record.get("primary_source")
    windows = record.get("source_request_windows")
    if (
        not isinstance(primary_source, str) or primary_source not in {"akshare", "tencent"}
        or not isinstance(windows, dict) or set(windows) != {primary_source, "baostock"}
    ):
        return unknown
    parsed = {}
    for source, role in ((primary_source, "primary"), ("baostock", "reference")):
        window = windows[source]
        if not isinstance(window, dict) or window.get("role") != role:
            return unknown
        dates = []
        for field in ("start_date", "end_date"):
            value = window.get(field)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None
            ):
                return unknown
            try:
                date.fromisoformat(value)
            except ValueError:
                return unknown
            dates.append(value)
        if dates[0] > dates[1]:
            return unknown
        parsed[role] = {"source": source, "start_date": dates[0], "end_date": dates[1]}
    primary, reference = parsed["primary"], parsed["reference"]
    left = (primary["start_date"], primary["end_date"])
    right = (reference["start_date"], reference["end_date"])
    relation = "same" if left == right else (
        "shorter" if left[0] <= right[0] and right[1] <= left[1] else "different"
    )
    explanations = {
        "same": "两侧请求范围相同；请求范围不能证明逐日数据完整。",
        "shorter": "对照请求范围短于主源；完整返回历史仍参与覆盖率计算。",
        "different": "两侧请求范围不同；完整返回历史仍参与覆盖率计算。",
    }
    return {"state": "available", **parsed, "relation": relation,
            "explanation": explanations[relation] + "窗口内逐日覆盖未计算，不能据此判为通过。"}


def _comparison(record: dict[str, Any]) -> dict[str, Any] | None:
    """Read bounded comparison facts, never provider messages or transport details."""
    value = record.get("source_comparison")
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("status") not in {"pass", "warning"}:
        raise ValueError("comparison_status")
    symbol = record.get("symbol")
    if symbol is not None and (
        not isinstance(symbol, str) or not re.fullmatch(r"(?:sh|sz|bj)\.[0-9]{6}", symbol)
    ):
        raise ValueError("comparison_symbol")
    safe = {"symbol": symbol, "status": value["status"]}
    for name in ("common_rows", "primary_only", "secondary_only"):
        count = value.get(name)
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("comparison_count")
        safe[name] = count
    ratio = value.get("coverage_ratio")
    if ratio is not None and (
        type(ratio) not in (int, float) or not 0 <= ratio <= 1 or not math.isfinite(ratio)
    ):
        raise ValueError("comparison_coverage")
    safe["coverage_ratio"] = ratio
    safe["coverage_scope"] = "all_returned_rows"
    safe["coverage_explanation"] = (
        "覆盖率为共同日期行数除以两侧规范化后参与比较的总行数较大值；"
        "不是供应商原始响应行数，未按共同请求窗口裁剪。"
        "覆盖率缺失时为未知；该统计不证明请求历史完整。"
    )
    safe["request_window_context"] = _request_window_context(record)
    return safe


def _source_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("summary")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records")
    successful = failed = 0
    offline = False
    evidence_failed = False
    comparisons: list[dict[str, Any] | None] = []
    for record in records:
        if not isinstance(record, dict) or not record:
            raise ValueError("record")
        comparisons.append(_comparison(record))
        rows, errors = record.get("rows"), record.get("source_errors")
        if type(rows) is not int or rows < 0 or not isinstance(errors, dict):
            raise ValueError("record_counts")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in errors.items()):
            raise ValueError("errors")
        successful += rows > 0
        failed += len(errors)
        offline |= "offline_recovery" in errors
        expected_record = "failed" if rows == 0 else "partial_sources" if errors else "success"
        if "status" in record and record["status"] != expected_record:
            raise ValueError("record_status")
        # Zero rows without any explained source failure cannot confirm fetch success.
        if rows == 0 and not errors:
            if record.get("status") != "failed":
                raise ValueError("record_outcome_unknown")
            evidence_failed = True
        diagnostics = record.get("source_diagnostics", {})
        if not isinstance(diagnostics, dict):
            raise ValueError("diagnostics")
        for source, detail in diagnostics.items():
            if not isinstance(source, str) or not isinstance(detail, dict):
                raise ValueError("diagnostics")
            if detail.get("status") not in {"success", "filtered", "failed"}:
                raise ValueError("diagnostic_status")
            if detail["status"] == "failed":
                evidence_failed = True
                if source not in errors:
                    raise ValueError("unrecorded_failure")
            elif source in errors:
                raise ValueError("diagnostic_conflict")
            for field in ("received_rows", "accepted_rows", "rejected_rows"):
                if type(detail.get(field)) is not int or detail[field] < 0:
                    raise ValueError("diagnostic_count")
            received, accepted, rejected = (
                detail["received_rows"],
                detail["accepted_rows"],
                detail["rejected_rows"],
            )
            if accepted > received or rejected != received - accepted:
                raise ValueError("diagnostic_count_conflict")
    outcome = (
        "failed"
        if successful == 0
        else "partial_sources"
        if failed or evidence_failed
        else "success"
    )
    for field, count in (("successful_instruments", successful), ("failed_sources", failed)):
        if field in payload and (type(payload[field]) is not int or payload[field] != count):
            raise ValueError("summary_count")
    if "status" in payload and payload["status"] != outcome:
        raise ValueError("summary_status")
    if evidence_failed and outcome == "success":
        raise ValueError("hidden_failure")
    warnings = [item for item in comparisons if item is not None and item["status"] == "warning"]
    return {
        "source_outcome": outcome,
        "source_outcome_origin": "recorded" if "status" in payload else "derived",
        "successful_instruments": successful,
        "failed_sources": failed,
        "successful_instruments_origin": "recorded"
        if "successful_instruments" in payload
        else "derived",
        "failed_sources_origin": "recorded" if "failed_sources" in payload else "derived",
        "offline_recovery_hint": offline,
        "source_comparison_state": "warning"
        if warnings else "pass" if all(item is not None for item in comparisons) else "unknown",
        "source_comparison_warning_count": len(warnings),
        "source_comparison_warnings": warnings,
    }


def _market(root: Path, observer: Observation, notes: list[dict[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "unknown",
        "publication_state": "unknown",
        "isolated": None,
        "batch_id": None,
        "batch_id_kind": "market_update_batch",
        "phase": None,
        "reason": None,
        "summary_state": "unknown",
        "summary_fingerprint_matched": None,
        "source_outcome": "unknown",
        "source_outcome_origin": "unknown",
        "successful_instruments": None,
        "failed_sources": None,
        "offline_recovery_hint": None,
        "source_comparison_state": "unknown",
        "source_comparison_warning_count": None,
        "source_comparison_warnings": [],
    }
    try:
        raw_marker = observer.read(root / "data" / "market_update_state.json", "market")
        try:
            raw_summary = observer.read(root / "outputs" / "update_summary.json", "market")
            result["summary_state"] = "missing" if raw_summary is None else "present"
        except (OSError, ValueError):
            raw_summary = None
            result["summary_state"] = "unavailable"
            notes.append(diagnostic("market_summary_unreadable", "market"))
        if raw_marker is None:
            result.update(state="legacy", publication_state="legacy", reason="marker_missing")
            notes.append(diagnostic("market_marker_missing", "market"))
            return result
        marker = parse_market_update_state(strict_json(raw_marker))
        result.update(
            state="available",
            publication_state=marker.status,
            batch_id=marker.batch_id,
            phase=marker.phase,
            isolated=marker.status != "completed",
        )
        if marker.status != "completed":
            result["reason"] = "incomplete_batch"
            notes.append(diagnostic("market_isolated", "market"))
            return result
        if raw_summary is None:
            unavailable = result["summary_state"] == "unavailable"
            result["reason"] = "summary_unreadable" if unavailable else "summary_missing"
            if not unavailable:
                notes.append(diagnostic("market_summary_missing", "market"))
            return result
        matched = hashlib.sha256(raw_summary).hexdigest() == marker.summary_sha256
        result["summary_fingerprint_matched"] = matched
        if not matched:
            result["reason"] = "summary_mismatch"
            notes.append(diagnostic("market_summary_mismatch", "market"))
            return result
        try:
            summary = strict_json(raw_summary)
            # The hint is safe presence evidence, even if the source semantics conflict.
            if isinstance(summary, dict) and isinstance(summary.get("records"), list):
                result["offline_recovery_hint"] = any(
                    isinstance(r, dict)
                    and isinstance(r.get("source_errors"), dict)
                    and "offline_recovery" in r["source_errors"]
                    for r in summary["records"]
                )
            result.update(_source_summary(summary))
        except (ValueError, TypeError, KeyError, RecursionError):
            notes.append(diagnostic("summary_conflict", "market"))
            result["reason"] = "summary_conflict"
        if result["source_outcome"] != "success":
            notes.append(diagnostic("market_sources_not_success", "market"))
        if result["source_comparison_state"] == "warning":
            notes.append(diagnostic("source_comparison_warning", "market"))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        result.update(state="unavailable", reason="market_evidence_invalid")
        notes.append(diagnostic("market_evidence_invalid", "market"))
    return result


def _ledger(
    root: Path, portfolio: str, observer: Observation, notes: list[dict[str, str]],
    ledger_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "unavailable",
        "audit_status": "unknown",
        "audit_scope": "all_paper_portfolios",
        "accounts": [],
        "last_valuation_date": None,
        "input_identity_kind": "ledger_input",
        "correlation": {
            "status": "not_available"
            if ledger_identity is None or ledger_identity["scope"] == "unknown"
            else "unavailable"
        },
    }
    database = root / "data" / "finance_lab.duckdb"
    try:
        wal = database.with_name(database.name + ".wal")
        # Capture absence as well as existence; never connect in the presence of a WAL.
        observer.read(wal, "ledger", limit=None)
        if safe_path(wal) is not None:
            notes.append(diagnostic("ledger_wal_present", "ledger"))
            return result
        observer.read(database, "ledger", limit=None)
        if safe_path(database) is None:
            notes.append(diagnostic("ledger_missing", "ledger"))
            return result
        with PaperStore(database, read_only=True) as store:
            connection = store._require_connection()
            # Disable disk spill before sorting/auditing, or fail closed.
            connection.execute("SET temp_directory = ''")
            connection.execute("BEGIN TRANSACTION")
            try:
                audit = store.audit_recovery_ledger()
                result["audit_status"] = audit["status"]
                if audit["status"] != "passed":
                    notes.append(diagnostic("ledger_audit_unavailable", "ledger"))
                    return result
                if not store.portfolio_exists(portfolio):
                    notes.append(diagnostic("ledger_portfolio_missing", "ledger"))
                    if (
                        ledger_identity is not None
                        and ledger_identity["scope"] in {"complete", "partial"}
                        and ledger_identity["runs"]
                    ):
                        result["correlation"] = store.correlate_run_references(
                            portfolio, ledger_identity
                        )
                    return result
                snapshot = store.portfolio_snapshot(portfolio)
                if not snapshot["accounts"]:
                    notes.append(diagnostic("ledger_portfolio_missing", "ledger"))
                    return result
                accounts: list[dict[str, Any]] = []
                for account in snapshot["accounts"]:
                    row = connection.execute(
                        "SELECT MAX(trade_date) FROM paper_events "
                        "WHERE account_id = ? AND event_type = 'VALUATION'",
                        [account["account_id"]],
                    ).fetchone()
                    valuation = row[0].isoformat() if row and row[0] is not None else None
                    state = account["state"]
                    run = account["latest_run"]
                    accounts.append(
                        {
                            "account_id": account["account_id"],
                            "symbol": account["symbol"],
                            "cash": state["cash"],
                            "shares": state["shares"],
                            "equity": state["equity"],
                            "last_event_hash": state["last_event_hash"],
                            "last_state_hash": run["state_hash"],
                            "last_valuation_date": valuation,
                            "latest_run": {
                                key: run[key] for key in ("run_id", "batch_id", "trade_date")
                            },
                            "ledger_input_identity": {
                                key: run[key] for key in ("dataset_id", "curated_sha256")
                            },
                        }
                    )
                dates = {a["last_valuation_date"] for a in accounts}
                common = next(iter(dates)) if len(dates) == 1 else None
                result.update(state="available", accounts=accounts, last_valuation_date=common)
                if ledger_identity is not None:
                    result["correlation"] = store.correlate_run_references(
                        portfolio, ledger_identity
                    )
                if common is None:
                    notes.append(
                        diagnostic("ledger_valuation_dates_unknown_or_different", "ledger")
                    )
            finally:
                connection.execute("ROLLBACK")
    except Exception:
        result.update(state="unavailable", accounts=[], last_valuation_date=None)
        if ledger_identity is not None and ledger_identity["scope"] != "unknown":
            result["correlation"] = {"status": "unavailable"}
        notes.append(diagnostic("ledger_unavailable", "ledger"))
    return result


def _report(automation: dict[str, Any]) -> dict[str, Any]:
    latest = automation["latest_attempt"]
    state = "unknown"
    if latest is not None:
        if latest["status"] == "success" and latest["report_path_recorded"]:
            state = "generated"
        elif latest["failed_step"] == "paper_report":
            state = "failed"
        elif latest["failed_step"] in {"market_data_update", "data_health"}:
            state = "not_reached"
    return {
        "generation_state": state,
        "artifact_verified": False,
        "source": "latest_valid_unambiguous_attempt",
        "report_path_accessed": False,
    }


def _error(code: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "error",
        "command_exit_code": 2,
        **_SAFETY,
        "diagnostics": [{"code": code, "severity": "error"}],
    }


def _invalidate_sections(
    automation: dict[str, Any], market: dict[str, Any], ledger: dict[str, Any], changed: set[str],
    *, notes: list[dict[str, str]],
) -> None:
    """Use the same withdrawal rules for the inner and final observation checks."""
    for section in sorted(changed):
        if section == "automation":
            automation.update(
                state="changed", latest_attempt=None, last_success=None,
                latest_candidates=[], last_success_candidates=[],
            )
            automation["scan"]["complete"] = False
            automation["scan"]["visible_archive_count"] = None
            automation["latest_mirror"] = {"state": "changed"}
        elif section == "market":
            market.update(
                state="changed", publication_state="unknown", batch_id=None, phase=None,
                reason="input_changed", isolated=None, summary_state="changed",
                summary_fingerprint_matched=None, source_outcome="unknown",
                source_outcome_origin="unknown", successful_instruments=None,
                failed_sources=None, successful_instruments_origin="unknown",
                failed_sources_origin="unknown", offline_recovery_hint=None,
                source_comparison_state="unknown", source_comparison_warning_count=None,
                source_comparison_warnings=[],
            )
        elif section == "ledger":
            ledger.update(
                state="changed", audit_status="unknown", accounts=[], last_valuation_date=None
            )
    if changed and ledger["correlation"]["status"] != "not_available":
        ledger["correlation"] = {"status": "unavailable", "reason": "input_changed"}
    if "market" in changed:
        notes[:] = [item for item in notes if item["code"] != "source_comparison_warning"]


def _invalidate_recorded_observation(
    result: dict[str, Any], changed: set[str]
) -> dict[str, Any]:
    """Withdraw derived facts after a later check of the same observation inputs."""
    changed = changed & {"automation", "market", "ledger"}
    if not changed:
        return result
    automation, market, ledger = result["automation"], result["market"], result["ledger"]
    _invalidate_sections(automation, market, ledger, changed, notes=result["diagnostics"])
    for section in sorted(changed):
        note = diagnostic("input_changed", section)
        if note not in result["diagnostics"]:
            result["diagnostics"].append(note)
    result["automation_ledger_correlation"] = ledger["correlation"]["status"]
    result["report"] = _report(automation)
    result["observation"]["input_changed"] = True
    result.update(status="attention", command_exit_code=1)
    return result


def inspect_paper_operational_status(
    root: Path, portfolio_id: str, *, _observer: Observation | None = None
) -> dict[str, Any]:
    started = _now()
    try:
        portfolio = validate_portfolio_id(portfolio_id)
    except (TypeError, ValueError):
        return _error("portfolio_id_invalid")
    try:
        root = validate_root(root)
    except (OSError, ValueError):
        return _error("root_invalid")
    if _observer is not None and (
        not isinstance(_observer, Observation) or _observer.root != root
    ):
        return _error("observation_root_invalid")
    observer = Observation(root) if _observer is None else _observer
    notes: list[dict[str, str]] = []
    # Fixed directory memberships, including missing directories, are observation inputs.
    for directory, section in ((root / "data", "market"), (root / "outputs", "automation")):
        try:
            observer.listing(directory, section)
        except (OSError, ValueError):
            notes.append(diagnostic("directory_unavailable", section))
    automation = _inspect_history(root, portfolio, observer)
    notes.extend(automation.pop("diagnostics"))
    market = _market(root, observer, notes)
    latest_attempt = automation["latest_attempt"]
    identity = latest_attempt.get("run_identity") if latest_attempt is not None else None
    ledger_identity = identity["ledger"] if identity is not None else None
    ledger = _ledger(root, portfolio, observer, notes, ledger_identity)
    changed = observer.changed()
    for section in sorted(changed):
        notes.append(diagnostic("input_changed", section))
    _invalidate_sections(automation, market, ledger, changed, notes=notes)
    report = _report(automation)
    if changed and ledger_identity is not None and ledger_identity["scope"] != "unknown":
        ledger["correlation"] = {"status": "unavailable", "reason": "input_changed"}
    correlation = ledger["correlation"]["status"]
    if correlation in {"mismatch", "unavailable"}:
        notes.append(diagnostic("automation_ledger_" + correlation, "ledger"))
    latest = automation["latest_attempt"]
    if latest is not None:
        if latest["status"] != "success":
            notes.append(diagnostic("latest_attempt_failed", "automation"))
        if not latest["required_fields_known"]:
            notes.append(diagnostic("legacy_fields_unknown", "automation"))
    if report["generation_state"] != "generated":
        notes.append(diagnostic("report_not_confirmed_generated", "report"))
    result = {
        "schema_version": 1,
        "status": "attention" if notes else "ok",
        "command_exit_code": 1 if notes else 0,
        **_SAFETY,
        "automation_ledger_correlation": correlation,
        "observation": {
            "root": str(root),
            "portfolio_id": portfolio,
            "started_at": started,
            "completed_at": _now(),
            "consistency": "non_atomic",
            "input_changed": bool(changed),
            "limitations": [
                "ABA and changes after final check are not excluded",
                "not a cross-tenant TOCTOU security boundary",
            ],
        },
        "automation": automation,
        "ledger": ledger,
        "market": market,
        "report": report,
        "diagnostics": notes,
    }
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return _error("json_unavailable")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--portfolio", required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_paper_operational_status(args.root, args.portfolio)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except Exception:
        result = _error("json_unavailable")
        encoded = json.dumps(result)
    print(encoded)
    return int(result["command_exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
