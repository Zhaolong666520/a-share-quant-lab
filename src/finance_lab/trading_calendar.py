from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


class CalendarConfigurationError(ValueError):
    """The tracked SSE holiday snapshot cannot be used safely."""


@dataclass(frozen=True)
class TradingDayFreshness:
    trading_days: int
    used_weekday_fallback: bool
    method: str


@dataclass(frozen=True)
class SSETradingCalendar:
    calendar_id: str
    coverage_start: date
    coverage_end: date
    closed_weekdays: frozenset[date]
    source_urls: tuple[str, ...]
    sha256: str

    def days_after(self, last_date: date, as_of_date: date) -> TradingDayFreshness:
        if as_of_date <= last_date:
            return TradingDayFreshness(
                trading_days=0,
                used_weekday_fallback=False,
                method=self.calendar_id,
            )

        first_date = last_date + timedelta(days=1)
        used_weekday_fallback = (
            first_date < self.coverage_start or as_of_date > self.coverage_end
        )
        count = 0
        current = first_date
        while current <= as_of_date:
            if current.weekday() < 5 and (
                current < self.coverage_start
                or current > self.coverage_end
                or current not in self.closed_weekdays
            ):
                count += 1
            current += timedelta(days=1)

        method = self.calendar_id
        if used_weekday_fallback:
            method += "_with_weekday_fallback"
        return TradingDayFreshness(
            trading_days=count,
            used_weekday_fallback=used_weekday_fallback,
            method=method,
        )


def _parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise CalendarConfigurationError(f"{field} 必须是 ISO 日期字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CalendarConfigurationError(f"{field} 不是有效日期：{value!r}") from exc


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CalendarConfigurationError(f"{field} 必须是非空字符串")
    return value.strip()


def load_sse_trading_calendar(path: Path) -> SSETradingCalendar:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalendarConfigurationError(f"无法读取交易日历快照：{path}") from exc
    if not isinstance(payload, dict):
        raise CalendarConfigurationError("交易日历根节点必须是对象")
    if _required_string(payload, "exchange") != "SSE":
        raise CalendarConfigurationError("交易日历 exchange 必须为 SSE")

    calendar_id = _required_string(payload, "calendar_id")
    coverage_start = _parse_date(payload.get("coverage_start"), "coverage_start")
    coverage_end = _parse_date(payload.get("coverage_end"), "coverage_end")
    if coverage_start > coverage_end:
        raise CalendarConfigurationError("coverage_start 不得晚于 coverage_end")

    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CalendarConfigurationError("sources 必须是非空数组")
    source_urls: list[str] = []
    source_years: set[int] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise CalendarConfigurationError("sources 中每项必须是对象")
        year = source.get("year")
        url = source.get("url")
        if type(year) is not int or not isinstance(url, str) or not url.startswith(
            "https://www.sse.com.cn/"
        ):
            raise CalendarConfigurationError("sources 必须提供上交所 HTTPS URL 和年份")
        source_years.add(year)
        source_urls.append(url)
    expected_years = set(range(coverage_start.year, coverage_end.year + 1))
    if source_years != expected_years:
        raise CalendarConfigurationError("sources 必须覆盖快照中的每个自然年")

    raw_closed_dates = payload.get("closed_weekdays")
    if not isinstance(raw_closed_dates, list):
        raise CalendarConfigurationError("closed_weekdays 必须是数组")
    closed_dates = tuple(
        _parse_date(value, "closed_weekdays") for value in raw_closed_dates
    )
    if len(set(closed_dates)) != len(closed_dates):
        raise CalendarConfigurationError("closed_weekdays 不得有重复日期")
    if any(
        item < coverage_start or item > coverage_end or item.weekday() >= 5
        for item in closed_dates
    ):
        raise CalendarConfigurationError("closed_weekdays 必须是覆盖范围内的周一至周五")

    return SSETradingCalendar(
        calendar_id=calendar_id,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        closed_weekdays=frozenset(closed_dates),
        source_urls=tuple(source_urls),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_project_sse_trading_calendar(project_root: Path) -> SSETradingCalendar:
    configured_path = project_root / "config" / "sse_trading_calendar.json"
    if configured_path.exists():
        return load_sse_trading_calendar(configured_path)
    bundled_path = Path(__file__).resolve().parents[2] / "config" / configured_path.name
    return load_sse_trading_calendar(bundled_path)
