"""Explicit, immutable source selection captured inside the market-data lock."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from finance_lab.config import Instrument

_LIMIT = 4096
_DEFAULT = b'{"schema_version":1,"profile":"legacy","configured":false}'
_FileIdentity = tuple[int, int, int, int]
_Snapshot = tuple[_FileIdentity, bytes] | None


class SourceProfileError(ValueError):
    """Invalid or changed configuration must never silently select legacy sources."""


def _identity(info: os.stat_result) -> _FileIdentity:
    # Windows path-stat and handle-stat can expose different ctime semantics.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _safe(info: os.stat_result, *, directory: bool) -> None:
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    ):
        raise SourceProfileError("source_profile 路径必须是无链接的普通文件和目录")


def _check_ancestors(root: Path) -> bool:
    for path in (*reversed(root.parents), root):
        _safe(path.lstat(), directory=True)
    try:
        info = (root / "config").lstat()
    except FileNotFoundError:
        return False
    _safe(info, directory=True)
    return True


def _snapshot(root: Path) -> _Snapshot:
    try:
        if not _check_ancestors(root):
            return None
        target = root / "config" / "source_profile.json"
        try:
            before = target.lstat()
        except FileNotFoundError:
            return None
        _safe(before, directory=False)
        if before.st_size > _LIMIT:
            raise SourceProfileError("source_profile 超过4096字节限制")
        with target.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            _safe(opened, directory=False)
            content = handle.read(_LIMIT + 1)
            finished = os.fstat(handle.fileno())
        _check_ancestors(root)
        after = target.lstat()
        _safe(after, directory=False)
        if (
            len(content) > _LIMIT or len(content) != before.st_size
            or not _identity(before) == _identity(opened) == _identity(finished) == _identity(after)
        ):
            raise SourceProfileError("source_profile 在读取期间发生变化")
        return _identity(after), content
    except OSError as exc:
        raise SourceProfileError("source_profile 无法安全读取；请保留配置并检查路径和权限") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON constant")


@dataclass(frozen=True)
class SourceProfile:
    root: Path
    profile_id: str
    config_sha256: str
    configured: bool
    primary_source: str
    required_sources: tuple[str, ...]
    _captured: _Snapshot = field(repr=False)

    def request_start(self, source: str, instrument: Instrument, start: date, end: date) -> date:
        if source not in self.required_sources:
            raise SourceProfileError("source_profile 不支持该来源")
        if self.profile_id == "tencent_baostock":
            if (instrument.exchange, instrument.code, instrument.kind) not in {
                ("sh", "000300", "index"), ("sh", "510300", "etf"),
            }:
                raise SourceProfileError("腾讯来源配置只支持 sh.000300:index 和 sh.510300:etf")
            if source == "baostock" and instrument.kind == "etf":
                start = max(start, date(2026, 1, 1))
                if end < start:
                    raise SourceProfileError("BaoStock ETF 核对范围不得早于2026-01-01")
        return start

    def metadata(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id, "config_sha256": self.config_sha256,
            "configured": self.configured, "required_sources": list(self.required_sources),
            "primary_source": self.primary_source,
        }

    def assert_unchanged(self) -> None:
        if _snapshot(self.root) != self._captured:
            raise SourceProfileError("source_profile 在行情更新期间发生变化；批次必须停止")


def load_source_profile(root: Path) -> SourceProfile:
    root = root.absolute()  # Do not resolve away symlinks before checking their ancestors.
    captured = _snapshot(root)
    profile = "legacy"
    if captured is not None:
        try:
            payload = json.loads(
                captured[1].decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if (
                not isinstance(payload, dict) or set(payload) != {"schema_version", "profile"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or not isinstance(payload["profile"], str)
                or payload["profile"] not in {"legacy", "tencent_baostock"}
            ):
                raise ValueError("invalid profile schema")
            profile = payload["profile"]
        except (ValueError, TypeError, RecursionError) as exc:
            raise SourceProfileError("source_profile JSON 无效；拒绝回退到默认来源") from exc
    primary = "akshare" if profile == "legacy" else "tencent"
    loaded = SourceProfile(
        root=root, profile_id=profile,
        config_sha256=hashlib.sha256(captured[1] if captured is not None else _DEFAULT).hexdigest(),
        configured=captured is not None, primary_source=primary,
        required_sources=(primary, "baostock"), _captured=captured,
    )
    loaded.assert_unchanged()
    return loaded
