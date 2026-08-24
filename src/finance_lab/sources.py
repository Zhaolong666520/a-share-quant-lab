from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from finance_lab.config import Instrument

CANONICAL_COLUMNS = [
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "volume_unit",
    "amount",
    "adjustment",
    "source",
    "ingested_at",
]


class DataSourceError(RuntimeError):
    """Raised when an upstream market data source cannot provide usable data."""


def _date_compact(value: date) -> str:
    return value.strftime("%Y%m%d")


def _date_iso(value: date) -> str:
    return value.isoformat()


def _ingested_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _numeric(frame: pd.DataFrame, columns: list[str]) -> None:
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")


def fetch_akshare(instrument: Instrument, start: date, end: date) -> pd.DataFrame:
    """Fetch unadjusted daily bars from documented AKShare public interfaces."""
    try:
        import akshare as ak

        if instrument.kind == "index":
            raw = ak.index_zh_a_hist(
                symbol=instrument.code,
                period="daily",
                start_date=_date_compact(start),
                end_date=_date_compact(end),
            )
        elif instrument.kind == "etf":
            raw = ak.fund_etf_hist_em(
                symbol=instrument.code,
                period="daily",
                start_date=_date_compact(start),
                end_date=_date_compact(end),
                adjust="",
            )
        else:
            raise DataSourceError(f"AKShare 暂不支持标的类型: {instrument.kind}")
    except DataSourceError:
        raise
    except Exception as exc:
        raise DataSourceError(f"AKShare 请求失败: {exc}") from exc

    if raw is None or raw.empty:
        raise DataSourceError(f"AKShare 未返回 {instrument.name} 的数据")

    required = {"日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"}
    missing = required.difference(raw.columns)
    if missing:
        raise DataSourceError(f"AKShare 字段发生变化，缺少: {sorted(missing)}")

    frame = raw.rename(
        columns={
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
    )[["trade_date", "open", "high", "low", "close", "volume", "amount"]].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    _numeric(frame, ["open", "high", "low", "close", "volume", "amount"])
    frame["symbol"] = instrument.symbol
    frame["volume_unit"] = "lot"
    frame["adjustment"] = "none"
    frame["source"] = "akshare"
    frame["ingested_at"] = _ingested_at()
    frame = frame.dropna(subset=["trade_date", "open", "high", "low", "close"])
    return frame[CANONICAL_COLUMNS].sort_values("trade_date").reset_index(drop=True)

def fetch_baostock(instrument: Instrument, start: date, end: date) -> pd.DataFrame:
    """Fetch unadjusted daily bars from BaoStock as an independent check source."""
    try:
        import baostock as bs

        login = bs.login()
        if login.error_code != "0":
            raise DataSourceError(f"BaoStock 登录失败: {login.error_msg}")
        try:
            fields = "date,code,open,high,low,close,volume,amount,tradestatus"
            result = bs.query_history_k_data_plus(
                instrument.baostock_code,
                fields,
                start_date=_date_iso(start),
                end_date=_date_iso(end),
                frequency="d",
                adjustflag="3",
            )
            if result.error_code != "0":
                raise DataSourceError(f"BaoStock 请求失败: {result.error_msg}")
            rows: list[list[str]] = []
            while result.next():
                rows.append(result.get_row_data())
            raw = pd.DataFrame(rows, columns=result.fields)
        finally:
            bs.logout()
    except DataSourceError:
        raise
    except Exception as exc:
        raise DataSourceError(f"BaoStock 请求失败: {exc}") from exc

    if raw.empty:
        raise DataSourceError(f"BaoStock 未返回 {instrument.name} 的数据")

    frame = raw.rename(columns={"date": "trade_date"}).copy()
    if "tradestatus" in frame.columns:
        frame = frame[frame["tradestatus"].isin(["", "1"])]
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    _numeric(frame, ["open", "high", "low", "close", "volume", "amount"])
    frame["symbol"] = instrument.symbol
    frame["volume_unit"] = "share"
    frame["adjustment"] = "none"
    frame["source"] = "baostock"
    frame["ingested_at"] = _ingested_at()
    frame = frame.dropna(subset=["trade_date", "open", "high", "low", "close"])
    return frame[CANONICAL_COLUMNS].sort_values("trade_date").reset_index(drop=True)
