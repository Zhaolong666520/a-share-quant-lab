"""Offline evidence copies: integrity verification, never repair or activation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from finance_lab import __version__
from finance_lab.atomic_io import write_text_atomic
from finance_lab.config import ProjectPaths
from finance_lab.market_data_lock import market_data_lock
from finance_lab.market_data_state import inspect_market_update_status
from finance_lab.paper_lock import paper_run_lock
from finance_lab.paper_store import PaperStore

_LOCKS = {"data/paper_trading.lock", "data/market_data.lock"}
_ROOTS = ("config", "data", "outputs", "outputs/update_summary.json")
_METADATA = {"snapshot", "recovery_manifest.json", "recovery_check.json"}


class RecoveryError(RuntimeError):
    """Recovery materials cannot be safely prepared or verified."""

    def __init__(self, message: str, *, stage: str = "validation") -> None:
        super().__init__(message)
        self.stage = stage


def _paths(root: Path) -> ProjectPaths:
    return ProjectPaths(
        root,
        root / "data",
        root / "data/raw",
        root / "data/curated",
        root / "outputs",
        root / "data/finance_lab.duckdb",
    )


def _safe_path(path: Path) -> Path:
    # Check before resolve, otherwise a junction/symlink would disappear from view.
    absolute = Path(os.path.abspath(path.expanduser()))
    for item in (*reversed(absolute.parents), absolute):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RecoveryError("拒绝链接或重解析点")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RecoveryError("拒绝特殊文件")
    return absolute.resolve()


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or PurePosixPath(value).is_absolute():
        raise RecoveryError("清单相对路径无效")
    for part in value.split("/"):
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", part)
        ):
            raise RecoveryError("清单路径包含不安全名称")
    return value


def _hash(path: Path) -> str:
    _safe_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(root: Path, *, source: bool) -> dict[str, Any]:
    files: list[dict[str, object]] = []
    directories: list[str] = []
    names: set[str] = set()

    def visit(path: Path) -> None:
        _safe_path(path)
        name = _relative(path.relative_to(root).as_posix())
        if source and name in _LOCKS:
            return
        folded = name.casefold()
        if folded in names:
            raise RecoveryError("材料包含大小写路径别名")
        names.add(folded)
        if path.is_dir():
            directories.append(name)
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            files.append({"path": name, "size": path.stat().st_size, "sha256": _hash(path)})
        else:
            raise RecoveryError("材料文件在清点时消失")

    if source:
        for name in ("config", "data"):
            path = root / name
            _safe_path(path)
            if path.exists():
                if not path.is_dir():
                    raise RecoveryError("配置和数据根必须是目录")
                visit(path)
        outputs = root / "outputs"
        _safe_path(outputs)
        if outputs.exists():
            if not outputs.is_dir():
                raise RecoveryError("输出根必须是目录")
            directories.append("outputs")
            summary = outputs / "update_summary.json"
            _safe_path(summary)
            if summary.exists():
                if not summary.is_file():
                    raise RecoveryError("更新摘要必须是文件")
                visit(summary)
    else:
        for child in sorted(root.iterdir()):
            visit(child)
    return {
        "files": sorted(files, key=lambda row: str(row["path"])),
        "directories": sorted(directories),
        "missing": sorted(name for name in _ROOTS if not (root / name).exists()),
    }


def _no_wal(root: Path) -> None:
    if os.path.lexists(root / "data/finance_lab.duckdb.wal"):
        raise RecoveryError("检测到数据库 WAL；关闭相关程序后检查，不要删除 WAL 或强制恢复")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError("清单包含重复字段")
        result[key] = value
    return result


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "bundle_id",
        "captured_at",
        "files",
        "directories",
        "missing",
    }:
        raise RecoveryError("清单结构无效")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise RecoveryError("清单版本不受支持")
    if not isinstance(payload["bundle_id"], str) or not re.fullmatch(
        r"[a-f0-9]{32}", payload["bundle_id"]
    ):
        raise RecoveryError("材料包标识无效")
    if (
        not isinstance(payload["captured_at"], str)
        or datetime.fromisoformat(payload["captured_at"]).utcoffset() is None
    ):
        raise RecoveryError("清单时间无效")
    names: set[str] = set()
    for key in ("files", "directories", "missing"):
        if not isinstance(payload[key], list):
            raise RecoveryError("清单列表无效")
        ordered: list[str] = []
        for item in payload[key]:
            if key == "files":
                if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                    raise RecoveryError("文件条目无效")
                if (
                    type(item["size"]) is not int
                    or item["size"] < 0
                    or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                ):
                    raise RecoveryError("文件大小或指纹无效")
                name = _relative(item["path"])
            else:
                name = _relative(item)
            folded = name.casefold()
            if folded in names:
                raise RecoveryError("清单包含重复或冲突路径")
            names.add(folded)
            ordered.append(name)
            if key == "missing":
                if name not in _ROOTS:
                    raise RecoveryError("缺失项不受支持")
            elif not (
                name == "config"
                or name.startswith("config/")
                or name == "data"
                or name.startswith("data/")
                or name in {"outputs", _ROOTS[-1]}
            ):
                raise RecoveryError("清单超出材料范围")
            if name in _LOCKS or name == "data/finance_lab.duckdb.wal":
                raise RecoveryError("材料不能包含锁或 WAL")
            if (key == "files" and name in {"config", "data", "outputs"}) or (
                key == "directories" and name == _ROOTS[-1]
            ):
                raise RecoveryError("材料根或摘要类型错误")
        if ordered != sorted(ordered):
            raise RecoveryError("清单顺序不规范")
    return payload


def _write_json(path: Path, payload: object) -> None:
    _safe_path(path)
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


def _bundle_root(bundle: Path) -> Path:
    root = _safe_path(bundle)
    if not root.is_dir():
        raise RecoveryError("材料目录不存在")
    for path in root.iterdir():
        _safe_path(path)
        if path.name not in _METADATA:
            raise RecoveryError("材料目录包含未知条目")
        if path.name != "snapshot" and not path.is_file():
            raise RecoveryError("材料元数据必须是文件")
    if not (root / "snapshot").is_dir():
        raise RecoveryError("材料缺少 snapshot 目录")
    return root


def verify_recovery(bundle: Path, *, record: bool = True) -> dict[str, Any]:
    """Recompute every digest; never trust an earlier check result."""
    root = _bundle_root(bundle)
    manifest_hash: str | None = None
    try:
        manifest = root / "recovery_manifest.json"
        manifest_hash = _hash(manifest)
        payload = _load_manifest(manifest)
        snapshot = root / "snapshot"
        expected = {key: payload[key] for key in ("files", "directories", "missing")}
        _no_wal(snapshot)
        if _inventory(snapshot, source=False) != expected:
            raise RecoveryError("副本集合或指纹不匹配")
        paths = _paths(snapshot)
        ledger: dict[str, object] = {"status": "not_present", "accounts": [], "event_count": 0}
        if paths.database.exists():
            try:
                with PaperStore(paths.database, read_only=True) as store:
                    ledger = store.audit_recovery_ledger()
            except Exception as exc:
                # Do not expose SQL errors, serialized events or data in diagnostics.
                ledger = {"status": "failed", "error_type": type(exc).__name__}
        market = inspect_market_update_status(paths)
        if _inventory(snapshot, source=False) != expected or _hash(manifest) != manifest_hash:
            raise RecoveryError("检查期间材料发生变化")
        _no_wal(snapshot)
        result = {
            "schema_version": 1,
            "manifest_sha256": manifest_hash,
            "checked_at": datetime.now(UTC).isoformat(),
            "program_version": __version__,
            "copy_integrity": "verified",
            "ledger_audit": ledger,
            "market_status": market,
            "ready_for_use": False,
            "ok": ledger["status"] in {"passed", "not_present"},
        }
        if record:
            _write_json(root / "recovery_check.json", result)
        return result
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "manifest_sha256": manifest_hash,
            "checked_at": datetime.now(UTC).isoformat(),
            "program_version": __version__,
            "copy_integrity": "failed",
            "ready_for_use": False,
            "ok": False,
            "error_type": type(exc).__name__,
        }
        if record:
            try:
                _write_json(root / "recovery_check.json", failure)
            except (OSError, RecoveryError):
                pass  # Preserve the original failure, not a failure to record it.
        raise RecoveryError(
            "恢复材料校验失败；保留目录，不可切换使用", stage="verification"
        ) from exc


def prepare_recovery(source: Path, destination: Path) -> dict[str, Any]:
    """Copy selected evidence under both project locks, never opening the source DB."""
    root, target = _safe_path(source), _safe_path(destination)
    if not root.is_dir() or not (root / "data").is_dir():
        raise RecoveryError("源项目及 data 目录必须已存在；不会初始化或创建源数据目录")
    if (
        target.exists()
        or root == target
        or target.is_relative_to(root)
        or root.is_relative_to(target)
        or not target.parent.is_dir()
    ):
        raise RecoveryError("目标必须是项目之外、父目录已存在的全新目录")
    paths = _paths(root)
    # Inspect potential lock paths before a lock implementation could follow them.
    for name in _LOCKS:
        _safe_path(root / name)
    with paper_run_lock(paths.data / "paper_trading.lock"), market_data_lock(paths.data):
        stage = "inventory"
        try:
            _no_wal(root)
            before = _inventory(root, source=True)
            stage = "destination"
            target.mkdir(exist_ok=False)
            stage = "copy"
            snapshot = target / "snapshot"
            snapshot.mkdir()
            for name in before["directories"]:
                (snapshot / name).mkdir(parents=True, exist_ok=True)
            for row in before["files"]:
                original = root / row["path"]
                _safe_path(original)
                copied = _safe_path(snapshot / row["path"])
                shutil.copyfile(original, copied)
            stage = "consistency"
            _no_wal(root)
            if (
                _inventory(root, source=True) != before
                or _inventory(snapshot, source=False) != before
            ):
                raise RecoveryError("复制期间源数据或副本发生变化")
            manifest = {
                "schema_version": 1,
                "bundle_id": uuid.uuid4().hex,
                "captured_at": datetime.now(UTC).isoformat(),
                **before,
            }
            stage = "manifest"
            _write_json(target / "recovery_manifest.json", manifest)
            stage = "verification"
            return verify_recovery(target)
        except Exception as exc:
            raise RecoveryError(
                f"恢复材料准备失败（{type(exc).__name__}）；保留目标目录，重试须使用新目录",
                stage=stage,
            ) from exc
