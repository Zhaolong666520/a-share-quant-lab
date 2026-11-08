"""Pure archive identity protocol; no pipeline, report or filesystem access."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date
from typing import Any

from finance_lab.paper_models import PaperRunReference

_REFERENCE_FIELDS = {
    "run_id",
    "batch_id",
    "account_id",
    "trade_date",
    "dataset_id",
    "curated_sha256",
    "state_hash",
}


def _opaque(value: Any) -> str:
    # Historical input identities are opaque, not necessarily hex hashes.
    if not isinstance(value, str) or not value or len(value) > 4096 or not value.isprintable():
        raise ValueError("identity")
    return value


def _hex(value: Any, length: int) -> str:
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError("identity_hash")
    return value


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("identity_date")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("identity_date")
    return result


def parse_references(value: Any) -> tuple[PaperRunReference, ...]:
    if not isinstance(value, list):
        raise ValueError("references")
    refs = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _REFERENCE_FIELDS:
            raise ValueError("reference_fields")
        run_id = item["run_id"]
        if not isinstance(run_id, str) or re.fullmatch(r"paper-run-[0-9a-f]{24}", run_id) is None:
            raise ValueError("ledger_run_id")
        refs.append(
            PaperRunReference(
                run_id,
                _opaque(item["batch_id"]),
                _opaque(item["account_id"]),
                _day(item["trade_date"]),
                _opaque(item["dataset_id"]),
                _opaque(item["curated_sha256"]),
                _hex(item["state_hash"], 64),
            )
        )
    keys = [(r.trade_date, r.account_id) for r in refs]
    if (
        keys != sorted(keys)
        or len(set(keys)) != len(keys)
        or len({r.run_id for r in refs}) != len(refs)
    ):
        raise ValueError("reference_order_or_duplicate")
    for day in {r.trade_date for r in refs}:
        if (
            len({(r.batch_id, r.dataset_id, r.curated_sha256) for r in refs if r.trade_date == day})
            != 1
        ):
            raise ValueError("reference_batch_inputs")
    return tuple(refs)


def references_json(refs: tuple[PaperRunReference, ...]) -> list[dict[str, Any]]:
    if type(refs) is not tuple or any(
        not isinstance(r, PaperRunReference) or type(r.trade_date) is not date for r in refs
    ):
        raise ValueError("receipt_type")
    result = [
        {**asdict(r), "trade_date": r.trade_date.isoformat()}
        for r in sorted(refs, key=lambda r: (r.trade_date, r.account_id))
    ]
    parse_references(result)
    return result


def validate_market(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"batch_id", "summary_sha256"}:
        raise ValueError("market_identity")
    return {
        "batch_id": _hex(value["batch_id"], 32),
        "summary_sha256": _hex(value["summary_sha256"], 64),
    }


def validate_health(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"dataset_id", "as_of_date"}:
        raise ValueError("health_identity")
    _day(value["as_of_date"])
    return {"dataset_id": _opaque(value["dataset_id"]), "as_of_date": value["as_of_date"]}


def validate_report(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"archive_json_name", "archive_json_sha256"}:
        raise ValueError("report_identity")
    name = value["archive_json_name"]
    if (
        not isinstance(name, str)
        or len(name) > 255
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.json", name) is None
        or ".." in name
    ):
        raise ValueError("report_name")
    return {
        "archive_json_name": name,
        "archive_json_sha256": _hex(value["archive_json_sha256"], 64),
    }


def validate_run_identity(payload: dict[str, Any]) -> None:
    _hex(payload["attempt_id"], 32)
    value = payload["run_identity"]
    if not isinstance(value, dict) or set(value) != {"market", "health", "ledger", "report"}:
        raise ValueError("run_identity_fields")
    validate_market(value["market"])
    health = validate_health(value["health"])
    report = validate_report(value["report"])
    if health is not None and health["as_of_date"] != payload["as_of_date"]:
        raise ValueError("health_date")
    if health is not None and not any(s["name"] == "data_health" for s in payload["steps"]):
        raise ValueError("health_unreached")
    if report is not None and (payload["status"] != "success" or not payload["paper_report_path"]):
        raise ValueError("report_unreturned")
    ledger = value["ledger"]
    if not isinstance(ledger, dict) or set(ledger) != {"scope", "runs"}:
        raise ValueError("ledger_identity")
    scope = ledger["scope"]
    if scope not in {"complete", "partial", "unknown"}:
        raise ValueError("receipt_scope")
    refs = parse_references(ledger["runs"])
    operation = payload["operation_status"]
    if scope == "unknown":
        if refs or operation == "partial":
            raise ValueError("unknown_receipts")
        return
    if sorted({r.trade_date.isoformat() for r in refs}) != payload["processed_dates"]:
        raise ValueError("receipt_dates")
    if scope == "partial":
        if (
            not refs
            or operation != "partial"
            or payload["status"] != "failed"
            or payload["failed_step"] != "paper_run"
            or payload["ledger_committed"] is not True
        ):
            raise ValueError("partial_receipts")
    elif (
        operation not in {"initialized", "processed", "no-op"}
        or (not refs and operation != "no-op")
        or operation == "partial"
    ):
        raise ValueError("complete_receipts")
