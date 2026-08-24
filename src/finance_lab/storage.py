from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from finance_lab.config import ProjectPaths
from finance_lab.validation import assert_valid_daily_prices


def _safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def save_raw_snapshot(frame: pd.DataFrame, paths: ProjectPaths) -> Path:
    assert_valid_daily_prices(frame)
    symbol = str(frame["symbol"].iloc[0])
    source = str(frame["source"].iloc[0])
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = paths.raw / source / _safe_symbol(symbol)
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{timestamp}.parquet"
    frame.to_parquet(output, index=False)
    return output


def save_curated(frame: pd.DataFrame, paths: ProjectPaths) -> Path:
    assert_valid_daily_prices(frame)
    symbol = str(frame["symbol"].iloc[0])
    output = paths.curated / f"{_safe_symbol(symbol)}.parquet"
    if output.exists():
        previous = pd.read_parquet(output)
        combined = pd.concat([previous, frame], ignore_index=True)
        combined["trade_date"] = pd.to_datetime(combined["trade_date"])
        # A temporary fallback response must not overwrite an existing preferred-source row.
        source_priority = {"akshare": 20, "baostock": 10}
        combined["_source_priority"] = combined["source"].map(source_priority).fillna(0)
        combined = combined.sort_values(
            ["trade_date", "_source_priority", "ingested_at"]
        )
        frame = combined.drop_duplicates(["symbol", "trade_date"], keep="last")
        frame = frame.drop(columns="_source_priority")
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    assert_valid_daily_prices(frame)
    frame.to_parquet(output, index=False)
    return output


def sync_duckdb(paths: ProjectPaths) -> int:
    parquet_files = sorted(paths.curated.glob("*.parquet"))
    if not parquet_files:
        return 0
    escaped_files = [path.as_posix().replace(chr(39), chr(39) * 2) for path in parquet_files]
    file_list = ", ".join(f"'{path}'" for path in escaped_files)
    with duckdb.connect(str(paths.database)) as connection:
        connection.execute(
            f"""
            CREATE OR REPLACE TABLE daily_prices AS
            SELECT * FROM read_parquet([{file_list}], union_by_name = true)
            ORDER BY symbol, trade_date
            """
        )
        result = connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()
        if result is None:
            return 0
        count = int(result[0])
    return count


def read_curated(symbol: str, paths: ProjectPaths) -> pd.DataFrame:
    path = paths.curated / f"{_safe_symbol(symbol)}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"尚未找到 {symbol} 的整理数据，请先运行数据更新")
    frame = pd.read_parquet(path)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.sort_values("trade_date").reset_index(drop=True)
