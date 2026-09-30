"""Build and verify an isolated market candidate from explicitly selected raw files."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from finance_lab import __version__
from finance_lab.atomic_io import published_parquet_files, write_parquet_atomic
from finance_lab.config import load_instruments, project_root
from finance_lab.data_identity import assert_data_identity, canonical_symbol_stem
from finance_lab.paper_store import PaperStore
from finance_lab.recovery import (
    RecoveryError,
    _hash,
    _inventory,
    _load_manifest,
    _relative,
    _safe_path,
    _unique,
    _write_json,
    verify_recovery,
)
from finance_lab.sources import CANONICAL_COLUMNS
from finance_lab.validation import assert_valid_daily_prices, validate_daily_prices

_PAPER_TABLES = (
    "paper_schema_metadata",
    "paper_accounts",
    "paper_runs",
    "paper_events",
    "paper_state",
)


class RebuildError(RuntimeError):
    """An offline candidate could not be built or proved consistent."""


def _selection(path: Path, bundle: Path, manifest: dict[str, Any]) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "bundle_id", "items"}:
            raise RebuildError("选择文件结构错误")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise RebuildError("选择文件版本错误")
        if raw["bundle_id"] != manifest["bundle_id"] or not isinstance(raw["items"], list):
            raise RebuildError("选择文件与材料包不匹配")
        configured = {item.symbol for item in load_instruments(bundle / "snapshot")}
        if not configured or len(configured) != len(load_instruments(bundle / "snapshot")):
            raise RebuildError("配置标的重复或为空")
        recorded = {str(item["path"]): item["sha256"] for item in manifest["files"]}
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw["items"]:
            if not isinstance(item, dict) or set(item) != {"symbol", "raw_path", "sha256"}:
                raise RebuildError("快照选择条目不完整")
            symbol, name, digest = item["symbol"], _relative(item["raw_path"]), item["sha256"]
            if not isinstance(symbol, str) or symbol not in configured or symbol in seen:
                raise RebuildError("快照标的未配置或重复")
            stem = canonical_symbol_stem(symbol)
            parts = name.split("/")
            if (
                len(parts) != 5
                or parts[:2] != ["data", "raw"]
                or parts[3] != stem
                or not parts[4].endswith(".parquet")
            ):
                raise RebuildError("只接受所选标的的原始快照路径")
            if not isinstance(digest, str) or recorded.get(name) != digest:
                raise RebuildError("快照指纹未与材料清单匹配")
            seen.add(symbol)
            result.append({"symbol": symbol, "raw_path": name, "sha256": digest})
        if seen != configured:
            raise RebuildError("必须为全部配置标的各指定一份原始快照")
        return sorted(result, key=lambda item: item["symbol"])
    except (OSError, ValueError, TypeError, KeyError, RecoveryError) as exc:
        raise RebuildError("选择文件不可安全使用") from exc


def _read_raw(path: Path, symbol: str, source: str) -> tuple[pd.DataFrame, list[str]]:
    try:
        frame = pd.read_parquet(path)
        if set(CANONICAL_COLUMNS) - set(frame.columns):
            raise RebuildError("原始快照缺少必要字段")
        for field in ("open", "high", "low", "close", "volume", "amount"):
            if not pd.api.types.is_numeric_dtype(frame[field]):
                raise RebuildError("原始快照行情字段必须为数值类型")
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        warnings = [i.code for i in validate_daily_prices(frame) if i.severity == "warning"]
        assert_valid_daily_prices(frame)
        assert_data_identity(frame, expected_symbol=symbol)
        for field in ("source", "adjustment", "volume_unit", "ingested_at"):
            if frame[field].isna().any() or frame[field].astype(str).str.strip().eq("").any():
                raise RebuildError("原始快照元数据缺失")
        if set(frame["source"].astype(str)) != {source}:
            raise RebuildError("来源目录和行情来源不一致")
        if set(frame["adjustment"].astype(str)) != {"none"}:
            raise RebuildError("只支持同一不复权口径")
        if len(set(frame["volume_unit"].astype(str))) != 1:
            raise RebuildError("成交量单位混合")
        if pd.to_datetime(frame["ingested_at"], utc=True, errors="coerce").isna().any():
            raise RebuildError("采集时间无效")
        return frame.sort_values("trade_date").reset_index(drop=True), warnings
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise RebuildError("原始快照内容不可用于重建") from exc


def _ledger(database: Path) -> tuple[dict[str, object], dict[str, str]]:
    if not database.exists():
        return {"status": "not_present", "accounts": [], "event_count": 0}, {}
    with PaperStore(database, read_only=True) as store:
        audit = store.audit_recovery_ledger()
    if audit["status"] not in {"passed", "not_present"}:
        raise RebuildError("账本状态不支持重建")
    if audit["status"] == "not_present":
        return audit, {}
    fingerprints: dict[str, str] = {}
    with duckdb.connect(str(database), read_only=True) as connection:
        for name in _PAPER_TABLES:
            cursor = connection.execute(f"SELECT * FROM {name}")
            columns = [part[0] for part in cursor.description]
            rows = sorted(repr(tuple(row)) for row in cursor.fetchall())
            payload = json.dumps([columns, rows], ensure_ascii=False, separators=(",", ":"))
            fingerprints[name] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return audit, fingerprints


def _selected_frames(
    snapshot: Path, items: list[dict[str, str]]
) -> tuple[list[dict[str, object]], dict[str, pd.DataFrame]]:
    details: list[dict[str, object]] = []
    frames: dict[str, pd.DataFrame] = {}
    for item in items:
        path = snapshot / item["raw_path"]
        if _hash(path) != item["sha256"]:
            raise RebuildError("原始快照指纹变化")
        source = item["raw_path"].split("/")[2]
        frame, warnings = _read_raw(path, item["symbol"], source)
        dates = frame["trade_date"]
        details.append(
            {
                **item,
                "source": source,
                "rows": len(frame),
                "first_trade_date": dates.iloc[0].date().isoformat(),
                "last_trade_date": dates.iloc[-1].date().isoformat(),
                "volume_unit": str(frame["volume_unit"].iloc[0]),
                "warning_codes": sorted(set(warnings)),
            }
        )
        frames[item["symbol"]] = frame
    return details, frames


def _database_matches(snapshot: Path, details: list[dict[str, object]]) -> int:
    files = [
        (snapshot / "data/curated" / f"{canonical_symbol_stem(str(item['symbol']))}.parquet")
        for item in details
    ]
    with duckdb.connect(str(snapshot / "data/finance_lab.duckdb"), read_only=True) as connection:
        actual = connection.execute(
            "SELECT * FROM daily_prices ORDER BY symbol, trade_date"
        ).fetchall()
        expected = connection.execute(
            "SELECT * FROM read_parquet(?, union_by_name=true) ORDER BY symbol, trade_date",
            [[path.as_posix() for path in files]],
        ).fetchall()
    if actual != expected or len(actual) != sum(int(str(item["rows"])) for item in details):
        raise RebuildError("数据库行情与整理文件不一致")
    return len(actual)


def _preserved_copy_matches(
    snapshot: Path, manifest: dict[str, Any], items: list[dict[str, str]], *, rebuilt: bool
) -> None:
    """Bind all copied files to the source, except explicitly rebuilt market artifacts."""
    observed = _inventory(snapshot, source=False)
    expected_files = {str(row["path"]): row for row in manifest["files"]}
    actual_files = {str(row["path"]): row for row in observed["files"]}
    curated = {
        f"data/curated/{canonical_symbol_stem(item['symbol'])}.parquet" for item in items
    }
    changed = curated | {"data/finance_lab.duckdb"} if rebuilt else set()
    expected_names = set(expected_files) | (curated if rebuilt else set())
    if set(actual_files) != expected_names:
        raise RebuildError("候选文件集合与原材料不符")
    for name in set(expected_files) - changed:
        if actual_files[name] != expected_files[name]:
            raise RebuildError("候选保留文件与原材料不符")
    expected_directories = set(manifest["directories"])
    if rebuilt:
        expected_directories.add("data/curated")
    if set(observed["directories"]) != expected_directories:
        raise RebuildError("候选目录集合与原材料不符")
    if observed["missing"] != manifest["missing"]:
        raise RebuildError("候选缺失项与原材料不符")


def rebuild_candidate(bundle: Path, selection: Path, destination: Path) -> dict[str, Any]:
    """Build a quarantined candidate in a new directory and record verified evidence."""
    source, chosen, target = _safe_path(bundle), _safe_path(selection), _safe_path(destination)
    live_root = _safe_path(project_root())
    if (
        target.exists()
        or not target.parent.is_dir()
        or target.is_relative_to(source)
        or source.is_relative_to(target)
        or target.is_relative_to(chosen)
        or target.is_relative_to(live_root)
        or live_root.is_relative_to(target)
    ):
        raise RebuildError("候选目标必须是材料包和正式项目之外的全新目录")
    verified = verify_recovery(source, record=False)
    if not verified["ok"]:
        raise RebuildError("恢复材料账本检查未通过")
    manifest = _load_manifest(source / "recovery_manifest.json")
    items = _selection(chosen, source, manifest)
    details, _ = _selected_frames(source / "snapshot", items)
    baseline, paper_hashes = _ledger(source / "snapshot/data/finance_lab.duckdb")
    curated = source / "snapshot/data/curated"
    if curated.exists():
        known = {f"{canonical_symbol_stem(item['symbol'])}.parquet" for item in items}
        if {path.name for path in published_parquet_files(curated)} - known:
            raise RebuildError("原整理目录含未配置标的")
    try:
        shutil.copytree(source / "snapshot", target / "snapshot")
        shutil.copyfile(chosen, target / "candidate_selection.json")
        snapshot = target / "snapshot"
        _preserved_copy_matches(snapshot, manifest, items, rebuilt=False)
        details, frames = _selected_frames(snapshot, items)
        (snapshot / "data/curated").mkdir(parents=True, exist_ok=True)
        paths = [
            _safe_path(snapshot / "data/curated" / f"{canonical_symbol_stem(symbol)}.parquet")
            for symbol in sorted(frames)
        ]
        for symbol, path in zip(sorted(frames), paths, strict=True):
            write_parquet_atomic(frames[symbol], path)
        database = snapshot / "data/finance_lab.duckdb"
        with duckdb.connect(str(database)) as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    "CREATE OR REPLACE TABLE daily_prices AS "
                    "SELECT * FROM read_parquet(?, union_by_name=true) ORDER BY symbol, trade_date",
                    [[path.as_posix() for path in paths]],
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        after, after_hashes = _ledger(database)
        if after != baseline or after_hashes != paper_hashes:
            raise RebuildError("候选构建改变了模拟账本")
        rows = _database_matches(snapshot, details)
        if _hash(chosen) != _hash(target / "candidate_selection.json"):
            raise RebuildError("选择文件在构建期间发生变化")
        if not verify_recovery(source, record=False)["ok"]:
            raise RebuildError("原材料包在构建期间发生变化")
        _preserved_copy_matches(snapshot, manifest, items, rebuilt=True)
        report = {
            "schema_version": 1,
            "candidate_id": uuid.uuid4().hex,
            "built_at": datetime.now(UTC).isoformat(),
            "program_version": __version__,
            "status": "candidate_pending_verification",
            "ready_for_use": False,
            "bundle_id": manifest["bundle_id"],
            "bundle_manifest_sha256": _hash(source / "recovery_manifest.json"),
            "selection_sha256": _hash(chosen),
            "items": details,
            "database_rows": rows,
            "ledger_audit": after,
            "ledger_table_hashes": after_hashes,
            "snapshot_manifest": _inventory(snapshot, source=False),
        }
        _write_json(target / "candidate_report.json", report)
        verify_candidate(source, target, _allow_pending=True)
        report["status"] = "candidate_validated"
        _write_json(target / "candidate_report.json", report)
        return report
    except Exception as exc:
        raise RebuildError(f"候选重建失败（{type(exc).__name__}）；保留目标目录") from exc


def verify_candidate(
    bundle: Path, candidate: Path, *, _allow_pending: bool = False
) -> dict[str, Any]:
    """Recheck candidate bytes, selected inputs, database rows and ledger."""
    source, target = _safe_path(bundle), _safe_path(candidate)
    try:
        if not verify_recovery(source, record=False)["ok"] or not target.is_dir():
            raise RebuildError("原材料或候选不可用")
        if {p.name for p in target.iterdir()} != {
            "snapshot",
            "candidate_selection.json",
            "candidate_report.json",
        }:
            raise RebuildError("候选目录结构无效")
        report_path = _safe_path(target / "candidate_report.json")
        report = json.loads(report_path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
        manifest = _load_manifest(source / "recovery_manifest.json")
        if (
            not isinstance(report, dict)
            or set(report)
            != {
                "schema_version",
                "candidate_id",
                "built_at",
                "program_version",
                "status",
                "ready_for_use",
                "bundle_id",
                "bundle_manifest_sha256",
                "selection_sha256",
                "items",
                "database_rows",
                "ledger_audit",
                "ledger_table_hashes",
                "snapshot_manifest",
            }
            or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1
            or report.get("status")
            != ("candidate_pending_verification" if _allow_pending else "candidate_validated")
            or report.get("ready_for_use") is not False
            or report.get("bundle_id") != manifest["bundle_id"]
            or report.get("bundle_manifest_sha256") != _hash(source / "recovery_manifest.json")
            or report.get("selection_sha256") != _hash(target / "candidate_selection.json")
        ):
            raise RebuildError("候选报告与材料不一致")
        items = _selection(target / "candidate_selection.json", source, manifest)
        snapshot = target / "snapshot"
        _preserved_copy_matches(snapshot, manifest, items, rebuilt=True)
        if _inventory(snapshot, source=False) != report["snapshot_manifest"]:
            raise RebuildError("候选文件集合或指纹变化")
        selected, _ = _selected_frames(snapshot, items)
        if selected != report["items"]:
            raise RebuildError("候选快照与报告不一致")
        database = snapshot / "data/finance_lab.duckdb"
        audit, hashes = _ledger(database)
        source_audit, source_hashes = _ledger(source / "snapshot/data/finance_lab.duckdb")
        if (
            audit != source_audit
            or hashes != source_hashes
            or audit != report["ledger_audit"]
            or hashes != report["ledger_table_hashes"]
            or _database_matches(snapshot, selected) != report["database_rows"]
        ):
            raise RebuildError("候选账本或行情数据库未通过复核")
        for item in selected:
            path = (
                snapshot / "data/curated" / f"{canonical_symbol_stem(str(item['symbol']))}.parquet"
            )
            candidate_frame = pd.read_parquet(path)
            raw_frame, _ = _read_raw(
                snapshot / str(item["raw_path"]), str(item["symbol"]), str(item["source"])
            )
            pd.testing.assert_frame_equal(candidate_frame, raw_frame)
        if (
            _inventory(snapshot, source=False) != report["snapshot_manifest"]
            or not verify_recovery(source, record=False)["ok"]
        ):
            raise RebuildError("复核期间材料发生变化")
        return report
    except Exception as exc:
        raise RebuildError("候选校验失败；保留目录，不可切换使用") from exc
