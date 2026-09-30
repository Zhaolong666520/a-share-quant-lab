"""Independent, integrity-checked research workspaces across CLI invocations."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_lab import __version__
from finance_lab.data_manifest import generate_dataset_manifest
from finance_lab.market_data_lock import market_data_lock
from finance_lab.market_data_state import assert_market_data_readable
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


class SessionError(RuntimeError):
    """A session cannot be created or consumed safely."""


def new_destination(destination: Path, *sources: Path) -> Path:
    target = _safe_path(destination)
    for source in sources:
        source = _safe_path(source)
        if target == source or target.is_relative_to(source) or source.is_relative_to(target):
            raise SessionError("目标目录必须与来源完全独立")
    if target.exists() or not target.parent.is_dir():
        raise SessionError("目标必须不存在，且父目录已经存在")
    return target


def copy_inventory(source: Path, target: Path, inventory: dict[str, Any]) -> None:
    for directory in inventory["directories"]:
        (target / directory).mkdir(parents=True, exist_ok=True)
    for item in inventory["files"]:
        path = target / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_safe_path(source / item["path"]), _safe_path(path))


def create_session(
    source: Path, destination: Path, *, expected_dataset_id: str | None = None
) -> dict[str, Any]:
    source = _safe_path(source)
    target = new_destination(destination, source)
    paths = _paths(source)
    if not paths.data.is_dir() or os.path.lexists(paths.data / "research_session.json"):
        raise SessionError("来源不是可冻结的正式项目")
    with paper_run_lock(paths.data / "paper_trading.lock"), market_data_lock(paths.data):
        _no_wal(source)
        assert_market_data_readable(paths)
        manifest = generate_dataset_manifest(paths)
        if not manifest.files or manifest.health_status == "error":
            raise SessionError("数据集为空或存在错误，不可冻结")
        if expected_dataset_id is not None and manifest.dataset_id != expected_dataset_id:
            raise SessionError("来源数据集 ID 与确认值不一致")
        before = _inventory(source, source=True)
        target.mkdir()
        snapshot = target / "snapshot"
        snapshot.mkdir()
        session_id = uuid.uuid4().hex
        (snapshot / "data").mkdir()
        _write_json(snapshot / "data/research_session.json", {"session_id": session_id})
        copy_inventory(source, snapshot, before)
        copied = _inventory(snapshot, source=True)
        copied["files"] = [
            item for item in copied["files"] if item["path"] != "data/research_session.json"
        ]
        if _inventory(source, source=True) != before or copied != before:
            raise SessionError("复制期间来源或目标发生变化；保留未完成目录")
        # Report commands use get_paths(), which ensures these directories. Normalize
        # only the independent copy before freezing, not the source project.
        _paths(snapshot).ensure()
        receipt = {
            "schema_version": 1,
            "session_id": session_id,
            "created_at": datetime.now(UTC).isoformat(),
            "program_version": __version__,
            "dataset_id": manifest.dataset_id,
            "snapshot_manifest": _inventory(snapshot, source=True),
        }
        _write_json(target / "session.json", receipt)
        return verify_session(target)


def verify_session(session: Path) -> dict[str, Any]:
    root = _safe_path(session)
    if {p.name for p in root.iterdir()} != {"snapshot", "session.json"}:
        raise SessionError("研究会话目录结构不完整")
    receipt_hash = _hash(root / "session.json")
    receipt = json.loads(
        _safe_path(root / "session.json").read_text(encoding="utf-8"), object_pairs_hook=_unique
    )
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "schema_version",
            "session_id",
            "created_at",
            "program_version",
            "dataset_id",
            "snapshot_manifest",
        }
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or not isinstance(receipt["session_id"], str)
        or len(receipt["session_id"]) != 32
        or any(c not in "0123456789abcdef" for c in receipt["session_id"])
        or not isinstance(receipt["dataset_id"], str)
        or len(receipt["dataset_id"]) != 64
        or any(c not in "0123456789abcdef" for c in receipt["dataset_id"])
        or not isinstance(receipt["created_at"], str)
        or datetime.fromisoformat(receipt["created_at"]).utcoffset() is None
        or not isinstance(receipt["program_version"], str)
    ):
        raise SessionError("研究会话凭据无效")
    snapshot = root / "snapshot"
    if not _safe_path(snapshot / "data").is_dir():
        raise SessionError("研究会话数据目录不存在")
    with market_data_lock(_paths(snapshot).data):
        _no_wal(snapshot)
        marker = json.loads(
            _safe_path(snapshot / "data/research_session.json").read_text(),
            object_pairs_hook=_unique,
        )
        before = _inventory(snapshot, source=True)
        if (
            marker != {"session_id": receipt["session_id"]}
            or before != receipt["snapshot_manifest"]
        ):
            raise SessionError("冻结数据或配置已变化，禁止继续研究")
        manifest = generate_dataset_manifest(_paths(snapshot))
        if manifest.dataset_id != receipt["dataset_id"] or manifest.health_status == "error":
            raise SessionError("冻结数据集 ID 或健康检查不通过")
        if (
            _inventory(snapshot, source=True) != before
            or _hash(root / "session.json") != receipt_hash
        ):
            raise SessionError("校验期间冻结数据变化")
    return receipt
