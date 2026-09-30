"""Persistent quarantine for interrupted updates, not rollback or power-loss recovery.

Read/write guards must run inside market_data_lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from finance_lab.atomic_io import write_text_atomic
from finance_lab.config import ProjectPaths
from finance_lab.data_identity import canonical_symbol_stem
from finance_lab.market_data_lock import market_data_lock

_PHASES = {"preparing", "fetch", "raw", "curated", "database", "summary", "commit"}
_GUIDANCE = (
    "行情更新状态不可用；请运行 finance-lab data-update-status 查看诊断，"
    "保留数据与状态文件，不要删除标记或强制继续。"
)


class MarketDataStateError(RuntimeError):
    """A persisted batch cannot safely be consumed or replaced."""


@dataclass(frozen=True)
class MarketUpdateState:
    schema_version: int
    batch_id: str
    status: str
    start: str
    end: str
    symbols: tuple[str, ...]
    started_at: str
    phase: str
    completed_at: str | None = None
    summary_sha256: str | None = None
    error_type: str | None = None


def _state_path(paths: ProjectPaths) -> Path:
    return paths.data / "market_update_state.json"


def _summary_hash(paths: ProjectPaths) -> str:
    digest = hashlib.sha256()
    with (paths.outputs / "update_summary.json").open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed


def _validate_payload(payload: Any) -> MarketUpdateState:
    if not isinstance(payload, dict) or set(payload) != {f.name for f in fields(MarketUpdateState)}:
        raise ValueError("invalid fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported schema")
    for key in ("batch_id", "status", "start", "end", "phase"):
        if not isinstance(payload[key], str):
            raise ValueError("invalid string")
    if not re.fullmatch(r"[0-9a-f]{32}", payload["batch_id"]):
        raise ValueError("invalid batch id")
    if payload["status"] not in {"updating", "failed", "completed"}:
        raise ValueError("invalid status")
    if payload["phase"] not in _PHASES:
        raise ValueError("invalid phase")
    start, end = date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"])
    if start.isoformat() != payload["start"] or end.isoformat() != payload["end"] or start >= end:
        raise ValueError("invalid range")
    symbols = payload["symbols"]
    if not isinstance(symbols, list) or not all(isinstance(symbol, str) for symbol in symbols):
        raise ValueError("invalid symbols")
    for symbol in symbols:
        canonical_symbol_stem(symbol)
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate symbol")
    started = _timestamp(payload["started_at"])
    if payload["status"] == "completed":
        if (
            payload["phase"] != "commit"
            or payload["error_type"] is not None
            or _timestamp(payload["completed_at"]) < started
            or not isinstance(payload["summary_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["summary_sha256"])
        ):
            raise ValueError("invalid completion")
    else:
        if payload["completed_at"] is not None or payload["summary_sha256"] is not None:
            raise ValueError("incomplete batch has completion fields")
        error = payload["error_type"]
        if payload["status"] == "failed":
            if not isinstance(error, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,127}", error
            ):
                raise ValueError("invalid error type")
        elif error is not None:
            raise ValueError("updating batch has error")
    return MarketUpdateState(**{**payload, "symbols": tuple(symbols)})


def _read_state(paths: ProjectPaths) -> MarketUpdateState | None:
    path = _state_path(paths)
    try:
        path.lstat()  # A dangling symlink is not a legacy project.
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MarketDataStateError(_GUIDANCE) from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        return _validate_payload(payload)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise MarketDataStateError(_GUIDANCE) from exc


def _publish(paths: ProjectPaths, state: MarketUpdateState) -> None:
    payload = {**asdict(state), "symbols": list(state.symbols)}
    _validate_payload(payload)
    write_text_atomic(_state_path(paths), json.dumps(payload, ensure_ascii=False, indent=2))


def _assess(paths: ProjectPaths) -> tuple[MarketUpdateState | None, str | None]:
    state = _read_state(paths)
    if state is None:
        return None, None
    if state.status != "completed":
        return state, "incomplete_batch"
    try:
        if _summary_hash(paths) != state.summary_sha256:
            return state, "summary_mismatch"
    except OSError:
        return state, "summary_unreadable"
    return state, None


def assert_market_data_readable(paths: ProjectPaths) -> None:
    """Check completion even when called in the active writing thread."""
    _, reason = _assess(paths)
    if reason is not None:
        raise MarketDataStateError(_GUIDANCE)


def market_update_status(paths: ProjectPaths) -> dict[str, object]:
    """Diagnose without publishing anything or bypassing the project lock."""
    with market_data_lock(paths.data):
        return inspect_market_update_status(paths)


def _source_outcome(paths: ProjectPaths) -> str | None:
    """Read the already hash-checked summary without treating publication as fetch success."""
    try:
        payload = json.loads(
            (paths.outputs / "update_summary.json").read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            return "unknown"
        if payload.get("status") in {"success", "partial_sources", "failed"}:
            return str(payload["status"])
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            return "unknown"
        if not all(isinstance(item, dict) for item in records):
            return "unknown"
        successful = any(type(item.get("rows")) is int and item["rows"] > 0 for item in records)
        failed = any(bool(item.get("source_errors")) for item in records)
        return "failed" if not successful else "partial_sources" if failed else "success"
    except (OSError, ValueError, TypeError, RecursionError):
        return "unknown"


def inspect_market_update_status(paths: ProjectPaths) -> dict[str, object]:
    """Inspect an immutable copy, or live paths already protected by the caller's lock."""
    try:
        state, reason = _assess(paths)
    except MarketDataStateError:
        return {
            "status": "invalid",
            "blocked": True,
            "reason": "invalid_state",
            "source_outcome": None,
            "state": None,
            "message": _GUIDANCE,
        }
    source_outcome = _source_outcome(paths) if state and reason is None else None
    return {
        "status": state.status if state else "legacy",
        "blocked": reason is not None,
        "reason": reason,
        "source_outcome": source_outcome,
        "state": asdict(state) if state else None,
        "message": (
            _GUIDANCE if reason else
            "行情批次已提交，但本次数据源全部失败；旧数据未被视为本次成功。"
            if source_outcome == "failed" else
            "行情批次已提交，但本次部分数据源失败；请查看更新摘要。"
            if source_outcome == "partial_sources" else
            "发布完整性检查通过；行情健康仍需单独校验。"
            if state else
            "旧项目兼容模式；没有新协议批次记录，不证明历史更新完整。"
        ),
    }


def _owner_key(paths: ProjectPaths) -> tuple[int, int, str]:
    return os.getpid(), threading.get_ident(), os.path.normcase(str(paths.data.resolve()))


_active_batches: dict[tuple[int, int, str], MarketUpdateBatch] = {}


def assert_market_data_writable(paths: ProjectPaths) -> None:
    """Only the active batch's internal writes may enter an incomplete update."""
    assert_not_research_session(paths)
    batch = _active_batches.get(_owner_key(paths))
    if batch is None:
        assert_market_data_readable(paths)
    else:
        batch._check_active()


def assert_not_research_session(paths: ProjectPaths) -> None:
    if os.path.lexists(paths.data / "research_session.json"):
        raise MarketDataStateError("研究会话为只读快照，禁止更新行情或模拟账本")


class MarketUpdateBatch:
    def __init__(self, paths: ProjectPaths, state: MarketUpdateState) -> None:
        self.paths = paths
        self.state = state

    def _check_active(self) -> None:
        if _active_batches.get(_owner_key(self.paths)) is not self:
            raise MarketDataStateError(_GUIDANCE)
        if _read_state(self.paths) != self.state or self.state.status != "updating":
            raise MarketDataStateError(_GUIDANCE)

    def stage(self, phase: str) -> None:
        self._check_active()
        next_state = replace(self.state, phase=phase)
        _publish(self.paths, next_state)
        self.state = next_state

    def _complete(self) -> None:
        self.stage("commit")
        completed = replace(
            self.state,
            status="completed",
            completed_at=datetime.now(UTC).isoformat(),
            summary_sha256=_summary_hash(self.paths),
        )
        _publish(self.paths, completed)
        self.state = completed


@contextmanager
def market_update_batch(
    paths: ProjectPaths,
    start: date,
    end: date,
    symbols: tuple[str, ...],
) -> Iterator[MarketUpdateBatch]:
    with market_data_lock(paths.data):
        assert_not_research_session(paths)
        assert_market_data_readable(paths)
        state = MarketUpdateState(
            schema_version=1,
            batch_id=uuid.uuid4().hex,
            status="updating",
            start=start.isoformat(),
            end=end.isoformat(),
            symbols=symbols,
            started_at=datetime.now(UTC).isoformat(),
            phase="preparing",
        )
        _publish(paths, state)  # No data work or in-memory permission before this succeeds.
        batch = MarketUpdateBatch(paths, state)
        key = _owner_key(paths)
        _active_batches[key] = batch
        try:
            yield batch
            batch._complete()
        except BaseException as exc:
            try:
                batch._check_active()
                error_type = type(exc).__name__
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", error_type):
                    error_type = "BaseException"
                _publish(paths, replace(batch.state, status="failed", error_type=error_type))
            except BaseException:
                # Preserve the previous quarantine and the original failure.
                pass
            raise
        finally:
            _active_batches.pop(key, None)
