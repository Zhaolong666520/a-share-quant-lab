"""Small, read-only evidence capture helpers; no publication or lock acquisition."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ARCHIVES = 10_000


class EvidenceError(ValueError):
    """Only a fixed local code is exposed, never an underlying exception message."""


def strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise EvidenceError("json_duplicate_key")
            value[key] = item
        return value

    def constant(value: str) -> Any:
        raise EvidenceError("json_nonfinite")

    def finite(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise EvidenceError("json_nonfinite")
        return parsed

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("json_invalid") from exc


def safe_path(path: Path, *, directory: bool = False) -> os.stat_result | None:
    """Check all ancestors before opening. This is not an adversarial TOCTOU boundary."""
    for candidate in reversed((path, *path.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EvidenceError("path_unreadable") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise EvidenceError("path_unsafe")
        expected_directory = candidate != path or directory
        if expected_directory and not stat.S_ISDIR(info.st_mode):
            raise EvidenceError("path_not_directory")
        if not expected_directory and not stat.S_ISREG(info.st_mode):
            raise EvidenceError("path_not_regular")
    return info


def validate_root(root: Path) -> Path:
    if ".." in root.parts:
        raise EvidenceError("root_parent_traversal")
    absolute = Path(os.path.abspath(root))
    if safe_path(absolute, directory=True) is None:
        raise EvidenceError("root_missing")
    return absolute


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def _capture(path: Path, limit: int | None) -> tuple[bytes | None, object]:
    info = safe_path(path)
    if info is None:
        return None, None
    if limit is not None and info.st_size > limit:
        raise EvidenceError("json_too_large")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(info):
            raise EvidenceError("input_replaced")
        digest = hashlib.sha256()
        blocks: list[bytes] = []
        count = 0
        while block := handle.read(64 * 1024):
            count += len(block)
            if limit is not None and count > limit:
                raise EvidenceError("json_too_large")
            digest.update(block)
            if limit is not None:
                blocks.append(block)
        ended = os.fstat(handle.fileno())
        after = safe_path(path)
        if (
            after is None
            or _identity(ended) != _identity(info)
            or _identity(after) != _identity(info)
        ):
            raise EvidenceError("input_replaced")
    return b"".join(blocks), (_identity(info), digest.hexdigest())


class Observation:
    """Remember captured bytes and relevant directory sets for one final comparison."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[Path, tuple[object, int | None, str]] = {}
        self.directories: dict[Path, tuple[object, str | None, str]] = {}

    def read(self, path: Path, section: str, *, limit: int | None = MAX_JSON_BYTES) -> bytes | None:
        try:
            raw, signature = _capture(path, limit)
        except EvidenceError:
            self.files[path] = ("unreadable", limit, section)
            raise
        except OSError as exc:
            self.files[path] = ("unreadable", limit, section)
            raise EvidenceError("evidence_unreadable") from exc
        self.files[path] = (signature, limit, section)
        return raw

    def listing(self, path: Path, section: str, prefix: str | None = None) -> list[Path] | None:
        info = safe_path(path, directory=True)
        if info is None:
            self.directories[path] = (None, prefix, section)
            return None
        candidates: list[Path] = []
        with os.scandir(path) as entries:
            for entry in entries:
                if prefix is None or entry.name.startswith(prefix):
                    candidates.append(path / entry.name)
                    if prefix is not None and len(candidates) > MAX_ARCHIVES:
                        self.directories[path] = ("over_limit", prefix, section)
                        raise EvidenceError("archive_limit")
        names = tuple(sorted(p.name for p in candidates))
        self.directories[path] = (names, prefix, section)
        return candidates

    def changed(self) -> set[str]:
        changed: set[str] = set()
        for path, (before, limit, section) in self.files.items():
            try:
                _, after = _capture(path, limit)
            except (OSError, EvidenceError):
                after = "unreadable"
            if before != after:
                changed.add(section)
        for path, (before, prefix, section) in list(self.directories.items()):
            try:
                info = safe_path(path, directory=True)
                if info is None:
                    after = None
                else:
                    names: list[str] = []
                    with os.scandir(path) as entries:
                        for entry in entries:
                            if prefix is None or entry.name.startswith(prefix):
                                names.append(entry.name)
                                if prefix is not None and len(names) > MAX_ARCHIVES:
                                    raise EvidenceError("archive_limit")
                    after = tuple(sorted(names))
            except (OSError, EvidenceError):
                after = "over_limit" if before == "over_limit" else "unreadable"
            if before != after:
                changed.add(section)
        return changed


def diagnostic(code: str, section: str) -> dict[str, str]:
    return {"code": code, "section": section, "severity": "attention"}
