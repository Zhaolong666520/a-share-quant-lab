"""Pure interval-baseline protocol; self hashes detect accidents, not authenticity."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

MAX_BASELINE_BYTES = 16 * 1024 * 1024
MAX_TABLE_ROWS = 50_000
MAX_ARCHIVES = 10_000
TABLE_IDENTIFIERS = {
    "paper_schema_metadata": "schema_version",
    "paper_accounts": "account_id",
    "paper_runs": "run_id",
    "paper_events": "event_id",
    "paper_state": "account_id",
}
BASELINE_FIELDS = {
    "schema_version",
    "workflow",
    "portfolio_id",
    "root_id",
    "captured_at",
    "ledger",
    "archives",
    "baseline_id",
}


class PaperAcceptanceRequestError(ValueError):
    """A fixed public request code, without raw caller or underlying error text."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def baseline_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_bytes({k: v for k, v in value.items() if k != "baseline_id"})
    ).hexdigest()


def moment(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo != UTC or result.isoformat().replace("+00:00", "Z") != value:
        raise ValueError("timestamp")
    return result


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError("identifier")
    return value


def hex_digest(value: Any, length: int = 64) -> str:
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError("hash")
    return value


def archive_name(value: Any, portfolio: str) -> str:
    if not isinstance(value, str):
        raise ValueError("archive_name")
    match = re.fullmatch(re.escape(portfolio) + r"_(\d{8}T\d{12}Z)_[0-9a-f]{16}\.json", value)
    if match is None:
        raise ValueError("archive_name")
    timestamp = datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ")
    if timestamp.strftime("%Y%m%dT%H%M%S%fZ") != match[1]:
        raise ValueError("archive_name")
    return value


def _validate(value: Any, root_id: str, portfolio_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != BASELINE_FIELDS:
        raise ValueError("fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("version")
    if value["workflow"] != "paper_run_acceptance_baseline":
        raise ValueError("workflow")
    if identifier(value["portfolio_id"]) != portfolio_id or hex_digest(value["root_id"]) != root_id:
        raise ValueError("identity")
    moment(value["captured_at"])
    ledger = value["ledger"]
    if not isinstance(ledger, dict) or set(ledger) != set(TABLE_IDENTIFIERS):
        raise ValueError("ledger")
    for table, key in TABLE_IDENTIFIERS.items():
        rows = ledger[table]
        if not isinstance(rows, list) or len(rows) > MAX_TABLE_ROWS:
            raise ValueError("rows")
        ids = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {key, "row_sha256"}:
                raise ValueError("row")
            item = row[key]
            if key == "schema_version":
                if type(item) is not int or item < 1:
                    raise ValueError("schema_version")
            else:
                identifier(item)
            hex_digest(row["row_sha256"])
            ids.append(item)
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise ValueError("row_order")
    if {row["account_id"] for row in ledger["paper_accounts"]} != {
        row["account_id"] for row in ledger["paper_state"]
    }:
        raise ValueError("state_roster")
    archives = value["archives"]
    if not isinstance(archives, list) or len(archives) > MAX_ARCHIVES:
        raise ValueError("archives")
    names = []
    for row in archives:
        if not isinstance(row, dict) or set(row) != {"archive_name", "sha256"}:
            raise ValueError("archive")
        names.append(archive_name(row["archive_name"], portfolio_id))
        hex_digest(row["sha256"])
    if names != sorted(names) or len(set(names)) != len(names):
        raise ValueError("archive_order")
    if hex_digest(value["baseline_id"]) != baseline_digest(value):
        raise ValueError("baseline_hash")
    if len(canonical_bytes(value)) > MAX_BASELINE_BYTES:
        raise ValueError("baseline_limit")
    return value


def validate_baseline(value: Any, root_id: str, portfolio_id: str) -> dict[str, Any]:
    """Validate the original fields, bounded canonical JSON and exact interval identity."""
    try:
        return _validate(value, root_id, portfolio_id)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        raise PaperAcceptanceRequestError("baseline_invalid") from exc
