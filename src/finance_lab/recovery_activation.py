"""Explicit candidate acceptance into a new project; never overwrite live data."""

from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from finance_lab.data_manifest import _generate_dataset_manifest, generate_dataset_manifest
from finance_lab.market_data_lock import market_data_lock
from finance_lab.market_data_state import MarketUpdateState, _publish, assert_market_data_readable
from finance_lab.paper_lock import paper_run_lock
from finance_lab.recovery import (
    _hash,
    _inventory,
    _no_wal,
    _paths,
    _safe_path,
    _unique,
    _write_json,
)
from finance_lab.recovery_rebuild import _database_matches, _ledger, verify_candidate
from finance_lab.research_session import copy_inventory, new_destination


def _accepted_inventory(root: Path) -> dict[str, Any]:
    inventory = _inventory(root, source=True)
    # Active marker is checked separately; the receipt is published before completion.
    inventory["files"] = [
        item for item in inventory["files"] if item["path"] != "data/market_update_state.json"
    ]
    return inventory


class ActivationError(RuntimeError):
    """A candidate did not meet the manual acceptance contract."""


def activate_candidate(
    bundle: Path, candidate: Path, destination: Path, *, confirm_candidate_id: str
) -> dict[str, Any]:
    bundle, candidate = _safe_path(bundle), _safe_path(candidate)
    target = new_destination(destination, bundle, candidate, _paths_current_root())
    with ExitStack() as locks:
        # Immutable evidence is never changed to add lock files; verify it before and after.
        report = verify_candidate(bundle, candidate)
        if report["candidate_id"] != confirm_candidate_id:
            raise ActivationError("必须人工确认完整 candidate_id")
        snapshot = candidate / "snapshot"
        before = _inventory(snapshot, source=True)
        target.mkdir()
        paths = _paths(target)
        paths.data.mkdir()
        locks.enter_context(paper_run_lock(paths.data / "paper_trading.lock"))
        locks.enter_context(market_data_lock(paths.data))
        now = datetime.now(UTC).isoformat()
        first = min(item["first_trade_date"] for item in report["items"])
        last = max(item["last_trade_date"] for item in report["items"])
        # The state contract requires a nonempty range; keep its upper boundary
        # separate from the actual final bar reported in the recovery summary.
        state_end = (
            last if first < last else (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        )
        pending = MarketUpdateState(
            1,
            report["candidate_id"],
            "updating",
            first,
            state_end,
            tuple(item["symbol"] for item in report["items"]),
            now,
            "preparing",
        )
        _publish(paths, pending)
        try:
            # Preserve the original quarantine marker as evidence, never publish it as active state.
            filtered = {
                **before,
                "files": [
                    item
                    for item in before["files"]
                    if item["path"] != "data/market_update_state.json"
                ],
            }
            copy_inventory(snapshot, target, filtered)
            (target / "recovery_evidence").mkdir()
            for name in ("candidate_report.json", "candidate_selection.json"):
                _write_json(
                    target / "recovery_evidence" / name,
                    json.loads(
                        (candidate / name).read_text(encoding="utf-8"), object_pairs_hook=_unique
                    ),
                )
            marker = snapshot / "data/market_update_state.json"
            if marker.exists():
                from shutil import copyfile

                copyfile(marker, target / "recovery_evidence/original_market_update_state.json")
            _write_json(
                paths.outputs / "update_summary.json",
                {
                    "status": "partial_sources",
                    "start": first,
                    "end": last,
                    "records": [
                        {
                            "symbol": item["symbol"],
                            "rows": item["rows"],
                            "curated_file": (
                                f"data/curated/{item['symbol'].replace('.', '_')}.parquet"
                            ),
                            "source_errors": {"offline_recovery": "离线恢复，不代表最新抓取成功"},
                        }
                        for item in report["items"]
                    ],
                },
            )
            _no_wal(target)
            audit, hashes = _ledger(paths.database)
            if audit != report["ledger_audit"] or hashes != report["ledger_table_hashes"]:
                raise ActivationError("模拟账本未通过无损验收")
            if _database_matches(target, report["items"]) != report["database_rows"]:
                raise ActivationError("行情数据库未通过验收")
            for item in filtered["files"]:
                if (
                    item["path"] != "outputs/update_summary.json"
                    and _hash(target / item["path"]) != item["sha256"]
                ):
                    raise ActivationError("恢复副本指纹不一致")
            if verify_candidate(bundle, candidate) != report:
                raise ActivationError("验收期间候选变化")
            completed = replace(
                pending,
                status="completed",
                phase="commit",
                completed_at=now,
                summary_sha256=_hash(paths.outputs / "update_summary.json"),
            )
            # Health inspection of the isolated, locked target is permitted before publication.
            # Public readers still see the pending state and remain blocked throughout.
            manifest = _generate_dataset_manifest(paths, None, 3, recovery_preflight=True)
            if not manifest.files or manifest.health_status == "error":
                raise ActivationError("恢复数据健康检查不通过")
            receipt = {
                "schema_version": 1,
                "candidate_id": report["candidate_id"],
                "bundle_id": report["bundle_id"],
                "accepted_at": now,
                "dataset_id": manifest.dataset_id,
                "market_state": {**asdict(completed), "symbols": list(completed.symbols)},
                "ledger_table_hashes": hashes,
                "snapshot_manifest": _accepted_inventory(target),
                "evidence_manifest": _inventory(target / "recovery_evidence", source=False),
                "source_outcome": "offline_recovery",
                "ready_for_use": True,
            }
            _write_json(target / "recovery_acceptance.json", receipt)
            _publish(paths, completed)  # Last publication makes the accepted project readable.
            return _verify_activation_locked(target)
        except BaseException:
            try:
                _publish(paths, replace(pending, status="failed", error_type="AcceptanceFailed"))
            except BaseException:
                pass  # Keep the pending quarantine and original error if publication also fails.
            raise


def _paths_current_root() -> Path:
    from finance_lab.config import project_root

    return project_root()


def verify_activation(root: Path) -> dict[str, Any]:
    root = _safe_path(root)
    paths = _paths(root)
    if not paths.data.is_dir() or not _safe_path(root / "recovery_acceptance.json").is_file():
        raise ActivationError("恢复目录或验收凭据不存在")
    with paper_run_lock(paths.data / "paper_trading.lock"), market_data_lock(paths.data):
        return _verify_activation_locked(root)


def _verify_activation_locked(root: Path) -> dict[str, Any]:
    paths = _paths(root)
    receipt_hash = _hash(root / "recovery_acceptance.json")
    receipt = json.loads(
        _safe_path(root / "recovery_acceptance.json").read_text(encoding="utf-8"),
        object_pairs_hook=_unique,
    )
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "schema_version",
            "candidate_id",
            "bundle_id",
            "accepted_at",
            "dataset_id",
            "market_state",
            "ledger_table_hashes",
            "snapshot_manifest",
            "evidence_manifest",
            "source_outcome",
            "ready_for_use",
        }
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
        or receipt.get("ready_for_use") is not True
        or receipt.get("source_outcome") != "offline_recovery"
    ):
        raise ActivationError("恢复验收凭据无效")
    _no_wal(root)
    assert_market_data_readable(paths)
    before = _accepted_inventory(root)
    if before != receipt["snapshot_manifest"]:
        raise ActivationError("恢复目录已变化；需重新核验，不可使用旧验收结论")
    if _inventory(root / "recovery_evidence", source=False) != receipt["evidence_manifest"]:
        raise ActivationError("恢复证据指纹变化")
    state = json.loads((paths.data / "market_update_state.json").read_text(encoding="utf-8"))
    if (
        state != receipt["market_state"]
        or state["batch_id"] != receipt["candidate_id"]
        or state["status"] != "completed"
    ):
        raise ActivationError("验收凭据不是当前恢复批次")
    manifest = generate_dataset_manifest(paths)
    audit, hashes = _ledger(paths.database)
    if (
        manifest.dataset_id != receipt["dataset_id"]
        or manifest.health_status == "error"
        or hashes != receipt["ledger_table_hashes"]
        or audit["status"] not in {"passed", "not_present"}
    ):
        raise ActivationError("恢复数据或账本验收失败")
    if (
        _accepted_inventory(root) != before
        or _hash(root / "recovery_acceptance.json") != receipt_hash
        or _inventory(root / "recovery_evidence", source=False) != receipt["evidence_manifest"]
    ):
        raise ActivationError("验收期间目录变化")
    return receipt
