"""Strict local snapshots for same-symbol ETF share adjustments."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse


class ShareAdjustmentValidationError(ValueError):
    """A local share-adjustment snapshot is not safe to post."""


@dataclass(frozen=True)
class ShareAdjustment:
    action_id: str
    symbol: str
    effective_date: date
    ratio_numerator: int
    ratio_denominator: int
    source_url: str
    source_published_at: datetime
    ingested_at: datetime


_REQUIRED_COLUMNS = (
    "symbol",
    "effective_date",
    "ratio_numerator",
    "ratio_denominator",
    "source_url",
    "source_published_at",
    "ingested_at",
)
_MAX_RATIO_TERM = 1_000_000_000


def _value(row: dict[str, str | None], column: str) -> str:
    value = row.get(column)
    if value is None or not value.strip():
        raise ShareAdjustmentValidationError(f"份额调整快照缺少 {column}")
    return value.strip()


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ShareAdjustmentValidationError("effective_date 必须是 ISO 日期") from exc


def _timestamp(value: str, column: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ShareAdjustmentValidationError(f"{column} 必须是 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShareAdjustmentValidationError(f"{column} 必须带时区")
    return parsed


def _ratio(numerator_raw: str, denominator_raw: str) -> tuple[int, int]:
    try:
        numerator = int(numerator_raw)
        denominator = int(denominator_raw)
    except ValueError as exc:
        raise ShareAdjustmentValidationError("份额调整比例必须是整数分数") from exc
    if (
        str(numerator) != numerator_raw
        or str(denominator) != denominator_raw
        or numerator <= 0
        or denominator <= 0
        or numerator > _MAX_RATIO_TERM
        or denominator > _MAX_RATIO_TERM
    ):
        raise ShareAdjustmentValidationError("份额调整比例无效")
    common = math.gcd(numerator, denominator)
    normalized = numerator // common, denominator // common
    if normalized == (1, 1):
        raise ShareAdjustmentValidationError("份额调整比例不能等于 1")
    return normalized


def _identity(
    symbol: str,
    effective_date: date,
    numerator: int,
    denominator: int,
    source_url: str,
    source_published_at: datetime,
) -> str:
    canonical = json.dumps(
        {
            "symbol": symbol,
            "effective_date": effective_date.isoformat(),
            "ratio_numerator": numerator,
            "ratio_denominator": denominator,
            "source_url": source_url,
            "source_published_at": source_published_at.isoformat(),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def load_share_adjustments(
    path: Path,
    *,
    symbol: str,
) -> tuple[ShareAdjustment, ...]:
    """Load a single immutable same-symbol adjustment snapshot."""
    if not path.exists():
        return ()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or set(reader.fieldnames) != set(
                _REQUIRED_COLUMNS
            ):
                raise ShareAdjustmentValidationError("份额调整快照缺少规范字段")
            rows = list(reader)
    except OSError as exc:
        raise ShareAdjustmentValidationError(f"无法读取份额调整快照：{path}") from exc

    actions: list[ShareAdjustment] = []
    action_ids: set[str] = set()
    effective_dates: set[date] = set()
    for row in rows:
        if None in row:
            raise ShareAdjustmentValidationError("份额调整快照存在错位字段")
        row_symbol = _value(row, "symbol")
        if row_symbol != symbol:
            raise ShareAdjustmentValidationError("份额调整快照只能包含目标标的")
        effective_date = _date(_value(row, "effective_date"))
        numerator, denominator = _ratio(
            _value(row, "ratio_numerator"),
            _value(row, "ratio_denominator"),
        )
        source_url = _value(row, "source_url")
        parsed_url = urlparse(source_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ShareAdjustmentValidationError("source_url 必须是 HTTPS URL")
        source_published_at = _timestamp(
            _value(row, "source_published_at"), "source_published_at"
        )
        ingested_at = _timestamp(_value(row, "ingested_at"), "ingested_at")
        if source_published_at.date() > effective_date:
            raise ShareAdjustmentValidationError("公告时间不能晚于份额调整生效日")
        if source_published_at > ingested_at:
            raise ShareAdjustmentValidationError("采集时间不能早于公告时间")
        action_id = _identity(
            row_symbol,
            effective_date,
            numerator,
            denominator,
            source_url,
            source_published_at,
        )
        if action_id in action_ids:
            raise ShareAdjustmentValidationError("份额调整快照包含重复事件")
        if effective_date in effective_dates:
            raise ShareAdjustmentValidationError("同一生效日只能有一项份额调整")
        action_ids.add(action_id)
        effective_dates.add(effective_date)
        actions.append(
            ShareAdjustment(
                action_id=action_id,
                symbol=row_symbol,
                effective_date=effective_date,
                ratio_numerator=numerator,
                ratio_denominator=denominator,
                source_url=source_url,
                source_published_at=source_published_at,
                ingested_at=ingested_at,
            )
        )
    return tuple(sorted(actions, key=lambda item: (item.effective_date, item.action_id)))


def load_share_adjustment_snapshots(
    directory: Path,
    *,
    symbol: str,
) -> tuple[ShareAdjustment, ...]:
    """Combine append-only share-adjustment snapshots."""
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ShareAdjustmentValidationError("份额调整快照路径必须是目录")
    actions: list[ShareAdjustment] = []
    action_ids: set[str] = set()
    effective_dates: set[date] = set()
    for path in sorted(directory.glob("*.csv"), key=lambda item: item.name):
        for action in load_share_adjustments(path, symbol=symbol):
            if action.action_id in action_ids:
                raise ShareAdjustmentValidationError("多个份额调整快照包含重复事件")
            if action.effective_date in effective_dates:
                raise ShareAdjustmentValidationError("同一生效日只能有一项份额调整")
            action_ids.add(action.action_id)
            effective_dates.add(action.effective_date)
            actions.append(action)
    return tuple(sorted(actions, key=lambda item: (item.effective_date, item.action_id)))
