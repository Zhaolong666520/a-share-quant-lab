"""Strict local cash-distribution snapshots for forward paper trading."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse


class CashDistributionValidationError(ValueError):
    """A local cash-distribution snapshot is not safe to post."""


@dataclass(frozen=True)
class CashDistribution:
    action_id: str
    symbol: str
    record_date: date
    ex_date: date
    payment_date: date
    cash_per_share: float
    currency: str
    source_url: str
    source_published_at: datetime
    ingested_at: datetime


_REQUIRED_COLUMNS = (
    "symbol",
    "record_date",
    "ex_date",
    "payment_date",
    "cash_per_share",
    "currency",
    "source_url",
    "source_published_at",
    "ingested_at",
)


def _value(row: dict[str, str | None], column: str) -> str:
    value = row.get(column)
    if value is None or not value.strip():
        raise CashDistributionValidationError(f"分红快照缺少 {column}")
    return value.strip()


def _date(value: str, column: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CashDistributionValidationError(f"{column} 必须是 ISO 日期") from exc


def _timestamp(value: str, column: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CashDistributionValidationError(f"{column} 必须是 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CashDistributionValidationError(f"{column} 必须带时区")
    return parsed


def _identity(
    symbol: str,
    record_date: date,
    ex_date: date,
    payment_date: date,
    cash_per_share: float,
    currency: str,
    source_url: str,
    source_published_at: datetime,
) -> str:
    payload = {
        "symbol": symbol,
        "record_date": record_date.isoformat(),
        "ex_date": ex_date.isoformat(),
        "payment_date": payment_date.isoformat(),
        "cash_per_share": format(cash_per_share, ".17g"),
        "currency": currency,
        "source_url": source_url,
        "source_published_at": source_published_at.isoformat(),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _read_rows(path: Path) -> list[dict[str, str | None]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or set(reader.fieldnames) != set(
                _REQUIRED_COLUMNS
            ):
                raise CashDistributionValidationError("分红快照缺少规范字段")
            rows = list(reader)
            if any(None in row for row in rows):
                raise CashDistributionValidationError("分红快照存在错位字段")
            return rows
    except OSError as exc:
        raise CashDistributionValidationError(f"无法读取分红快照：{path}") from exc


def load_cash_distributions(path: Path, *, symbol: str) -> tuple[CashDistribution, ...]:
    """Load one symbol's local cash-distribution snapshot.

    Missing input means that no cash is posted. Any malformed supplied input is
    rejected rather than silently transformed into a cash movement.
    """
    if not path.exists():
        return ()

    actions: list[CashDistribution] = []
    action_ids: set[str] = set()
    for row in _read_rows(path):
        row_symbol = _value(row, "symbol")
        if row_symbol != symbol:
            raise CashDistributionValidationError("分红快照只能包含目标标的")
        record_date = _date(_value(row, "record_date"), "record_date")
        ex_date = _date(_value(row, "ex_date"), "ex_date")
        payment_date = _date(_value(row, "payment_date"), "payment_date")
        if record_date > ex_date or ex_date > payment_date:
            raise CashDistributionValidationError("分红日期必须满足 record <= ex <= payment")
        try:
            cash_per_share = float(_value(row, "cash_per_share"))
        except ValueError as exc:
            raise CashDistributionValidationError("cash_per_share 必须是数字") from exc
        if not math.isfinite(cash_per_share) or cash_per_share <= 0.0:
            raise CashDistributionValidationError("cash_per_share 必须是有限正数")
        currency = _value(row, "currency")
        if currency != "CNY":
            raise CashDistributionValidationError("当前只支持 CNY 现金分红")
        source_url = _value(row, "source_url")
        parsed_url = urlparse(source_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise CashDistributionValidationError("source_url 必须是 HTTPS URL")
        source_published_at = _timestamp(
            _value(row, "source_published_at"), "source_published_at"
        )
        ingested_at = _timestamp(_value(row, "ingested_at"), "ingested_at")
        if source_published_at > ingested_at:
            raise CashDistributionValidationError("采集时间不能早于来源发布时间")
        action_id = _identity(
            row_symbol,
            record_date,
            ex_date,
            payment_date,
            cash_per_share,
            currency,
            source_url,
            source_published_at,
        )
        if action_id in action_ids:
            raise CashDistributionValidationError("分红快照包含重复事件")
        action_ids.add(action_id)
        actions.append(
            CashDistribution(
                action_id=action_id,
                symbol=row_symbol,
                record_date=record_date,
                ex_date=ex_date,
                payment_date=payment_date,
                cash_per_share=cash_per_share,
                currency=currency,
                source_url=source_url,
                source_published_at=source_published_at,
                ingested_at=ingested_at,
            )
        )
    return tuple(
        sorted(
            actions,
            key=lambda item: (item.record_date, item.payment_date, item.action_id),
        )
    )


def load_cash_distribution_snapshots(
    directory: Path,
    *,
    symbol: str,
) -> tuple[CashDistribution, ...]:
    """Combine append-only CSV snapshots without modifying previous evidence."""
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise CashDistributionValidationError("分红快照路径必须是目录")
    actions: list[CashDistribution] = []
    action_ids: set[str] = set()
    for path in sorted(directory.glob("*.csv"), key=lambda item: item.name):
        for action in load_cash_distributions(path, symbol=symbol):
            if action.action_id in action_ids:
                raise CashDistributionValidationError("多个分红快照包含重复事件")
            action_ids.add(action.action_id)
            actions.append(action)
    return tuple(
        sorted(
            actions,
            key=lambda item: (item.record_date, item.payment_date, item.action_id),
        )
    )
