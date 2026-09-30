"""Completion evidence for a report's separately published local files."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from finance_lab.atomic_io import write_text_atomic

_SHA256 = re.compile(r"[0-9a-f]{64}")


def report_marker_path(html_path: Path) -> Path:
    return html_path.with_suffix(".bundle.json")


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _safe_member_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and name == Path(name).name
        and "/" not in name
        and "\\" not in name
    )


def publish_report_bundle(html_path: Path, members: tuple[Path, ...]) -> Path:
    """Publish hashes last; this records completion, not a multi-file transaction."""
    if not members or html_path not in members:
        raise ValueError("报告完成标记必须包含 HTML 主文件")
    names = [path.name for path in members]
    if len(set(names)) != len(names):
        raise ValueError("报告文件名不能重复")
    directory = html_path.parent.resolve()
    files: list[dict[str, object]] = []
    for path in sorted(members, key=lambda item: item.name):
        if (
            not _safe_member_name(path.name)
            or path.parent.resolve() != directory
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError("报告成员文件无效或不在同一目录")
        sha256, size = _sha256_and_size(path)
        files.append({"name": path.name, "sha256": sha256, "size_bytes": size})
    marker = report_marker_path(html_path)
    write_text_atomic(
        marker,
        json.dumps(
            {"schema_version": 1, "report_name": html_path.name, "files": files},
            ensure_ascii=False,
            indent=2,
        ),
    )
    return marker


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 键")
        result[key] = value
    return result


def verify_report_bundle(html_path: Path) -> dict[str, object]:
    """Read only: reject missing, malformed, altered or unsafe bundle members."""
    marker = report_marker_path(html_path)
    outcome: dict[str, object] = {"ok": False, "status": "marker_missing", "marker": str(marker)}
    if marker.is_symlink():
        outcome["status"] = "invalid_marker"
        return outcome
    if not marker.is_file():
        return outcome
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError):
        outcome["status"] = "invalid_marker"
        return outcome
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "report_name", "files"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["report_name"] != html_path.name
        or not isinstance(payload["files"], list)
        or not payload["files"]
    ):
        outcome["status"] = "invalid_marker"
        return outcome
    seen: set[str] = set()
    for item in payload["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "sha256", "size_bytes"}
            or not _safe_member_name(item["name"])
            or not isinstance(item["sha256"], str)
            or _SHA256.fullmatch(item["sha256"]) is None
            or type(item["size_bytes"]) is not int
            or item["size_bytes"] < 0
            or item["name"] in seen
        ):
            outcome["status"] = "invalid_marker"
            return outcome
        seen.add(item["name"])
        member = marker.parent / item["name"]
        if member.is_symlink() or not member.is_file():
            outcome["status"] = "member_missing"
            return outcome
        try:
            sha256, size = _sha256_and_size(member)
        except OSError:
            outcome["status"] = "member_unreadable"
            return outcome
        if sha256 != item["sha256"] or size != item["size_bytes"]:
            outcome["status"] = "hash_mismatch"
            return outcome
    if html_path.name not in seen:
        outcome["status"] = "invalid_marker"
        return outcome
    outcome["ok"] = True
    outcome["status"] = "verified"
    outcome["files_checked"] = len(seen)
    return outcome
