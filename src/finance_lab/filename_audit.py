"""Read-only, conservative portability audit of curated Parquet filenames."""

from __future__ import annotations

import re
import stat
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from finance_lab.recovery import _safe_path

_CANONICAL_FILENAME = re.compile(r"[a-z][a-z0-9]{0,15}_[0-9]{6}\.parquet")


def _portable_key(name: str) -> str:
    """Model common macOS/Windows aliases without changing any real filename."""
    return unicodedata.normalize("NFC", name).rstrip(" .").casefold()


def _is_candidate(name: str) -> bool:
    return _portable_key(name).endswith(".parquet")


def _is_legacy_staging(name: str) -> bool:
    return name.startswith(".") and name.endswith(".tmp.parquet")


def audit_filename_names(names: Iterable[str]) -> dict[str, object]:
    """Audit names independently of the host filesystem's case sensitivity."""
    issues: list[dict[str, object]] = []
    candidates: list[str] = []
    ignored_staging_count = 0
    for name in sorted(names):
        if _is_legacy_staging(name):
            ignored_staging_count += 1
            continue
        if not _is_candidate(name):
            continue
        candidates.append(name)
        if _CANONICAL_FILENAME.fullmatch(name) is None:
            issues.append({"code": "noncanonical_filename", "name": name})

    aliases: dict[str, list[str]] = defaultdict(list)
    for name in candidates:
        aliases[_portable_key(name)].append(name)
    for grouped in aliases.values():
        if len(grouped) > 1:
            issues.append({"code": "portable_alias_collision", "names": sorted(grouped)})

    return {
        "ok": not issues,
        "status": "passed" if not issues else "failed",
        "candidate_count": len(candidates),
        "ignored_staging_count": ignored_staging_count,
        "content_checked": False,
        "data_committed": False,
        "issues": issues,
    }


def audit_curated_filenames(directory: Path) -> dict[str, object]:
    """Inspect direct directory entries only; never follow candidate symlinks."""
    _safe_path(directory)  # Reject ancestor links and Windows junction/reparse points too.
    if not directory.is_dir():
        raise OSError("整理行情目录不存在或是符号链接")
    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    result = audit_filename_names(entry.name for entry in entries)
    issues = list(cast(list[dict[str, object]], result["issues"]))
    for entry in entries:
        if not _is_candidate(entry.name) or _is_legacy_staging(entry.name):
            continue
        attributes = getattr(entry.lstat(), "st_file_attributes", 0)
        if entry.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            issues.append({"code": "symlink_candidate", "name": entry.name})
        elif not entry.is_file():
            issues.append({"code": "not_regular_file", "name": entry.name})
    result["issues"] = issues
    result["ok"] = not issues
    result["status"] = "passed" if not issues else "failed"
    return result
