"""Owned Tencent unadjusted daily protocol adapter."""

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from typing import Any

import pandas as pd
import requests

from finance_lab.config import Instrument
from finance_lab.sources import CANONICAL_COLUMNS, DataSourceError, _normalize

_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_NUMERIC = ("open", "close", "high", "low", "volume", "amount")
_MAX_BYTES = 1024 * 1024
_MAX_ELAPSED = 45
_CHINA = timezone(timedelta(hours=8))
_Bar = tuple[date, float, float, float, float, float, float]


def _failure(stage: str, received: int = 0) -> DataSourceError:
    return DataSourceError(
        f"Tencent daily {stage} failed", stage=stage,
        received_rows=received, rejected_rows=received,
    )


def _request_body(session: requests.Session, params: dict[str, str]) -> bytes:
    """Bound decoded streaming bytes and elapsed time without changing routing."""
    started = monotonic()
    with session.get(_URL, params=params, timeout=(5, 15),
                     allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            response.raise_for_status()
            raise requests.HTTPError("Unexpected HTTP status")
        if monotonic() - started > _MAX_ELAPSED:
            raise requests.Timeout("Tencent response deadline exceeded")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if monotonic() - started > _MAX_ELAPSED:
                raise requests.Timeout("Tencent response deadline exceeded")
            if len(body) + len(chunk) > _MAX_BYTES:
                raise requests.RequestException("Tencent response exceeds byte limit")
            body.extend(chunk)
        if monotonic() - started > _MAX_ELAPSED:
            raise requests.Timeout("Tencent response deadline exceeded")
        return bytes(body)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _finite_json(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("Nonfinite JSON constant")


def _day_rows(body: bytes, variable: str, symbol: str) -> list[Any]:
    text = body.decode("utf-8").strip(" \t\r\n")
    prefix = variable + "="
    if text.startswith(prefix):
        text = text[len(prefix):]
    if text.endswith(";"):
        text = text[:-1]
    payload = json.loads(text, object_pairs_hook=_object, parse_constant=_reject_constant,
                         parse_float=_finite_json)
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise ValueError("Invalid response envelope")
    if payload["code"] != 0:
        raise ValueError("Provider rejected request")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
        raise ValueError("Missing requested symbol")
    rows = data[symbol].get("day")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Missing unadjusted daily rows")
    return rows


def _bar(row: Any, instrument: Instrument, upper: date, received: int) -> _Bar:
    if not isinstance(row, list) or len(row) < 9:
        raise _failure("response", received)
    if not isinstance(row[0], str) or any(
        type(row[index]) not in (str, int, float) for index in (1, 2, 3, 4, 5, 8)
    ):
        raise _failure("response", received)
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row[0]) is None:
            raise ValueError("Invalid daily date")
        day = date.fromisoformat(row[0])
        if day > upper:
            raise ValueError("Future daily date")
        open_, close, high, low, volume, amount = (
            float(row[index]) for index in (1, 2, 3, 4, 5, 8)
        )
        volume *= 100 if instrument.kind == "etf" else 1
        amount *= 10000
        values = (open_, close, high, low, volume, amount)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Nonfinite daily value")
        if min(open_, close, high, low) <= 0 or not low <= min(open_, close) <= max(
            open_, close
        ) <= high:
            raise ValueError("Invalid price bounds")
        if volume < 0 or amount < 0 or (volume > 0 and amount == 0):
            raise ValueError("Invalid volume or amount")
    except (ValueError, OverflowError) as exc:
        raise _failure("validation", received) from exc
    return day, *values


def fetch_tencent(instrument: Instrument, start: date, end: date) -> pd.DataFrame:
    """Fetch only approved unadjusted Tencent bars, failing closed on any bad row."""
    if (
        not isinstance(instrument, Instrument)
        or (instrument.exchange, instrument.code, instrument.kind)
        not in {("sh", "000300", "index"), ("sh", "510300", "etf")}
        or type(start) is not date or type(end) is not date
        or start > end or end.year - start.year + 1 > 25 or end.year >= 9999
    ):
        raise _failure("validation")
    symbol = instrument.exchange + instrument.code
    received = 0
    overlaps = 0
    bars: dict[date, _Bar] = {}
    windows: list[dict[str, str]] = []
    today = datetime.now(_CHINA).date()
    try:
        with requests.Session() as session:
            for year in range(start.year, end.year + 1):
                lower, upper = f"{year:04d}-01-01", f"{year + 1:04d}-12-31"
                windows.append({"start": lower, "end": upper})
                variable = f"kline_day{year}"
                params = {"_var": variable, "param": f"{symbol},day,{lower},{upper},640,",
                          "r": "0.8205512681390605"}
                body = _request_body(session, params)
                try:
                    rows = _day_rows(body, variable, symbol)
                except (ValueError, TypeError, RecursionError) as exc:
                    raise _failure("response", received) from exc
                received += len(rows)
                limit = min(date(year + 1, 12, 31), today)
                response_dates: set[date] = set()
                for row in rows:
                    bar = _bar(row, instrument, limit, received)
                    day = bar[0]
                    if day in response_dates:
                        raise _failure("validation", received)
                    response_dates.add(day)
                    if day in bars:
                        if bar != bars[day]:
                            raise _failure("validation", received)
                        overlaps += 1
                    else:
                        bars[day] = bar
    except DataSourceError:
        raise
    except Exception as exc:
        raise _failure("request", received) from exc
    selected = [bar for day, bar in bars.items() if start <= day <= end]
    if not selected:
        raise _failure("validation", received)
    raw = pd.DataFrame(selected, columns=["trade_date", *_NUMERIC])
    try:
        frame = _normalize(
            raw, instrument, start, end, source="tencent", volume_unit="share",
            date_field="trade_date", fields={n: n for n in _NUMERIC},
        )[CANONICAL_COLUMNS]
    except Exception as exc:
        raise _failure("validation", received) from exc
    frame.attrs["tencent_transport"] = {
        "protocol": "akshare_stock_zh_a_hist_tx_day_v1", "requested_start": start.isoformat(),
        "requested_end": end.isoformat(), "provider_windows": windows,
        "raw_received_rows": received, "unique_valid_rows": len(bars),
        "exact_overlap_rows": overlaps, "outside_requested_rows": len(bars) - len(selected),
    }
    return frame
