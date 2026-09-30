from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from finance_lab.atomic_io import (
    published_parquet_files,
    write_parquet_atomic,
    write_parquet_immutable,
)
from finance_lab.config import ProjectPaths
from finance_lab.data_identity import assert_data_identity, canonical_symbol_stem
from finance_lab.market_data_lock import market_data_lock
from finance_lab.market_data_state import assert_market_data_readable, assert_market_data_writable
from finance_lab.validation import assert_valid_daily_prices


def _safe_symbol(symbol: str) -> str:
    return _safe_path_component(canonical_symbol_stem(symbol), label="symbol")


def _safe_path_component(value: str, *, label: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > 128
        or value in {".", ".."}
        or any(not (character.isalnum() or character in "._-") for character in value)
    ):
        raise ValueError(f"{label} 含有不安全的路径字符")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{number}" for number in range(1, 10))
    reserved.update(f"LPT{number}" for number in range(1, 10))
    if value.split(".", maxsplit=1)[0].upper() in reserved:
        raise ValueError(f"{label} 是保留的路径名称")
    return value


def _contained_path(base: Path, *parts: str) -> Path:
    resolved_base = base.resolve()
    candidate = resolved_base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(resolved_base):
        raise ValueError("生成的路径越过预期数据目录")
    return candidate


def save_raw_snapshot(frame: pd.DataFrame, paths: ProjectPaths) -> Path:
    with market_data_lock(paths.data):
        assert_market_data_writable(paths)
        return _save_raw_snapshot(frame, paths)


def _save_raw_snapshot(frame: pd.DataFrame, paths: ProjectPaths) -> Path:
    assert_valid_daily_prices(frame)
    symbol = assert_data_identity(frame)
    source = _safe_path_component(str(frame["source"].iloc[0]), label="source")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = _contained_path(paths.raw, source, _safe_symbol(symbol))
    directory.mkdir(parents=True, exist_ok=True)
    output = _contained_path(directory, f"{timestamp}_{uuid.uuid4().hex}.parquet")
    write_parquet_immutable(frame, output)
    return output


def save_curated(frame: pd.DataFrame, paths: ProjectPaths) -> Path:
    with market_data_lock(paths.data):
        assert_market_data_writable(paths)
        return _save_curated(frame, paths)


def _save_curated(frame: pd.DataFrame, paths: ProjectPaths) -> Path:
    assert_valid_daily_prices(frame)
    symbol = assert_data_identity(frame)
    output = _contained_path(paths.curated, f"{_safe_symbol(symbol)}.parquet")
    if output.exists():
        previous = pd.read_parquet(output)
        assert_data_identity(previous, filename=output.name, expected_symbol=symbol)
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
    assert_data_identity(frame, filename=output.name, expected_symbol=symbol)
    write_parquet_atomic(frame, output)
    return output


def sync_duckdb(paths: ProjectPaths) -> int:
    with market_data_lock(paths.data):
        assert_market_data_writable(paths)
        return _sync_duckdb(paths)


def _sync_duckdb(paths: ProjectPaths) -> int:
    parquet_files = published_parquet_files(paths.curated)
    if not parquet_files:
        return 0
    escaped_files = [path.as_posix().replace(chr(39), chr(39) * 2) for path in parquet_files]
    file_list = ", ".join(f"'{path}'" for path in escaped_files)
    with duckdb.connect(str(paths.database)) as connection:
        # Hold the project lock across both identity preflight and table replacement.
        for path in parquet_files:
            identity = connection.execute(
                "SELECT symbol FROM read_parquet(?)", [path.as_posix()]
            ).fetchdf()
            assert_data_identity(identity, filename=path.name)
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
    with market_data_lock(paths.data):
        assert_market_data_readable(paths)
        return _read_curated(symbol, paths)


def _read_curated(symbol: str, paths: ProjectPaths) -> pd.DataFrame:
    path = _contained_path(paths.curated, f"{_safe_symbol(symbol)}.parquet")
    if not path.exists():
        raise FileNotFoundError(f"尚未找到 {symbol} 的整理数据，请先运行数据更新")
    frame = pd.read_parquet(path)
    assert_data_identity(frame, filename=path.name, expected_symbol=symbol)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.sort_values("trade_date").reset_index(drop=True)
