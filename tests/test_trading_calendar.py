from __future__ import annotations

from datetime import date
from pathlib import Path

from finance_lab.trading_calendar import (
    load_project_sse_trading_calendar,
    load_sse_trading_calendar,
)


def _calendar_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "sse_trading_calendar.json"


def test_sse_calendar_excludes_2026_spring_festival_closure() -> None:
    calendar = load_sse_trading_calendar(_calendar_path())

    freshness = calendar.days_after(date(2026, 2, 13), date(2026, 2, 24))

    assert freshness.trading_days == 1
    assert freshness.used_weekday_fallback is False
    assert freshness.method == "sse_holiday_snapshot_2022_2026"


def test_sse_calendar_counts_new_year_closure_at_start_of_coverage() -> None:
    calendar = load_sse_trading_calendar(_calendar_path())

    freshness = calendar.days_after(date(2021, 12, 31), date(2022, 1, 4))

    assert freshness.trading_days == 1
    assert freshness.used_weekday_fallback is False


def test_sse_calendar_marks_weekday_fallback_outside_snapshot_coverage() -> None:
    calendar = load_sse_trading_calendar(_calendar_path())

    freshness = calendar.days_after(date(2027, 1, 1), date(2027, 1, 5))

    assert freshness.trading_days == 2
    assert freshness.used_weekday_fallback is True
    assert freshness.method == "sse_holiday_snapshot_2022_2026_with_weekday_fallback"


def test_project_calendar_falls_back_to_the_bundled_snapshot(tmp_path: Path) -> None:
    calendar = load_project_sse_trading_calendar(tmp_path)

    assert calendar.sha256 == load_sse_trading_calendar(_calendar_path()).sha256
