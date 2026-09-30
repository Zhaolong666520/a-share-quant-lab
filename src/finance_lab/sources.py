from __future__ import annotations

import sys
from datetime import UTC, date, datetime

import numpy as np
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

    def __init__(
        self,
        message: str,
        *,
        stage: str = "unknown",
        received_rows: int = 0,
        accepted_rows: int = 0,
        rejected_rows: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.received_rows = received_rows
        self.accepted_rows = accepted_rows
        self.rejected_rows = (
            received_rows - accepted_rows if rejected_rows is None else rejected_rows
        )


def _date_compact(value: date) -> str:
    return value.strftime("%Y%m%d")


def _date_iso(value: date) -> str:
    return value.isoformat()


def _ingested_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


_NUMERIC = ("open", "high", "low", "close", "volume", "amount")


def _invalid(message: str, received: int) -> DataSourceError:
    return DataSourceError(
        message, stage="validation", received_rows=received, rejected_rows=received
    )


def _normalize(
    raw: pd.DataFrame,
    instrument: Instrument,
    start: date,
    end: date,
    *,
    source: str,
    volume_unit: str,
    date_field: str,
    fields: dict[str, str],
    rejected_nontrading: int = 0,
    received_rows: int | None = None,
) -> pd.DataFrame:
    received = len(raw) if received_rows is None else received_rows
    required = {date_field, *fields}
    if not required.issubset(raw.columns):
        raise DataSourceError(
            f"{source} 响应缺少必要字段",
            stage="response",
            received_rows=received,
            rejected_rows=received,
        )
    if raw.empty:
        raise DataSourceError(
            f"{source} 未返回有效交易行",
            stage="response",
            received_rows=received,
            rejected_rows=received,
        )
    frame = raw.rename(columns={date_field: "trade_date", **fields})[
        ["trade_date", *_NUMERIC]
    ].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    if frame["trade_date"].isna().any():
        raise _invalid(f"{source} 含无效交易日期", received)
    if frame["trade_date"].ne(frame["trade_date"].dt.normalize()).any():
        raise _invalid(f"{source} 日线日期包含非零时间", received)
    if frame["trade_date"].dt.date.lt(start).any() or frame["trade_date"].dt.date.gt(end).any():
        raise _invalid(f"{source} 含请求范围外日期", received)
    if frame["trade_date"].duplicated().any():
        raise _invalid(f"{source} 含重复交易日期", received)
    for name in _NUMERIC:
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
        if frame[name].isna().any() or not np.isfinite(
            frame[name].to_numpy(dtype=float)
        ).all():
            raise _invalid(f"{source} 含无效 {name} 数值", received)
    frame["symbol"] = instrument.symbol
    frame["volume_unit"] = volume_unit
    frame["adjustment"] = "none"
    frame["source"] = source
    frame["ingested_at"] = _ingested_at()
    frame = frame[CANONICAL_COLUMNS].sort_values("trade_date").reset_index(drop=True)
    frame.attrs["source_diagnostics"] = {
        "received_rows": received,
        "accepted_rows": len(frame),
        "rejected_rows": rejected_nontrading,
        "rejection_reasons": {"nontrading": rejected_nontrading} if rejected_nontrading else {},
    }
    return frame


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
            raise DataSourceError("AKShare 不支持标的类型", stage="request")
    except DataSourceError:
        raise
    except Exception as exc:
        raise DataSourceError("AKShare 请求失败", stage="request") from exc

    if raw is None or not isinstance(raw, pd.DataFrame):
        raise DataSourceError("AKShare 响应不是表格", stage="response", received_rows=0)
    return _normalize(
        raw, instrument, start, end, source="akshare", volume_unit="lot",
        date_field="日期", fields={
            "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
            "成交量": "volume", "成交额": "amount",
        },
    )

def fetch_baostock(instrument: Instrument, start: date, end: date) -> pd.DataFrame:
    """Fetch unadjusted daily bars from BaoStock as an independent check source."""
    try:
        import baostock as bs
    except Exception as exc:
        raise DataSourceError("BaoStock 不可用", stage="import") from exc
    try:
        login = bs.login()
    except Exception as exc:
        raise DataSourceError("BaoStock 登录调用失败", stage="login") from exc
    try:
        if login.error_code != "0":
            raise DataSourceError("BaoStock 登录失败", stage="login")
        try:
            fields = "date,code,open,high,low,close,volume,amount,tradestatus"
            try:
                result = bs.query_history_k_data_plus(
                    instrument.baostock_code,
                    fields,
                    start_date=_date_iso(start),
                    end_date=_date_iso(end),
                    frequency="d",
                    adjustflag="3",
                )
            except Exception as exc:
                raise DataSourceError("BaoStock 查询调用失败", stage="query") from exc
            if result.error_code != "0":
                raise DataSourceError("BaoStock 请求失败", stage="query")
            rows: list[list[str]] = []
            try:
                while result.next():
                    rows.append(result.get_row_data())
            except Exception as exc:
                raise DataSourceError(
                    "BaoStock 结果迭代失败", stage="iteration", received_rows=len(rows)
                ) from exc
            raw = pd.DataFrame(rows, columns=result.fields)
        finally:
            primary_failed = sys.exc_info()[0] is not None
            try:
                bs.logout()
            except Exception as exc:
                if not primary_failed:
                    raise DataSourceError("BaoStock 退出失败", stage="logout") from exc
    except DataSourceError:
        raise
    except Exception as exc:
        raise DataSourceError("BaoStock 请求失败", stage="request") from exc

    received = len(raw)
    if raw.empty:
        raise DataSourceError("BaoStock 未返回交易行", stage="response")
    required = {"date", "code", *_NUMERIC, "tradestatus"}
    if not required.issubset(raw.columns):
        raise DataSourceError(
            "BaoStock 响应缺少必要字段", stage="response",
            received_rows=received, rejected_rows=received,
        )
    if set(raw["code"].astype(str)) != {instrument.symbol}:
        raise _invalid("BaoStock 返回了非目标标的", received)
    if not raw["tradestatus"].isin(["", "0", "1"]).all():
        raise _invalid("BaoStock 含未知交易状态", received)
    trading = raw[raw["tradestatus"].isin(["", "1"])]
    return _normalize(
        trading, instrument, start, end, source="baostock", volume_unit="share",
        date_field="date", fields={name: name for name in _NUMERIC},
        rejected_nontrading=received - len(trading), received_rows=received,
    )
