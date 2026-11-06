from __future__ import annotations

import uuid
from datetime import UTC, datetime
from numbers import Number
from pathlib import Path

import duckdb
import pandas as pd

from finance_lab.atomic_io import (
    published_parquet_files,
    write_parquet_atomic,
    write_parquet_immutable,
)
from finance_lab.config import ProjectPaths
from finance_lab.curated_io import read_curated_parquet
from finance_lab.data_identity import assert_data_identity, canonical_symbol_stem
from finance_lab.market_data_lock import market_data_lock
from finance_lab.market_data_state import assert_market_data_readable, assert_market_data_writable
from finance_lab.validation import DataValidationError, assert_valid_daily_prices


def _assert_storage_metadata(frame: pd.DataFrame, *, raw_snapshot: bool = False) -> None:
    for column in ("source", "volume_unit", "adjustment"):
        if not frame[column].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        ).all():
            raise DataValidationError(f"{column} 必须是非空字符串")
    if not frame["adjustment"].eq("none").all():
        raise DataValidationError("当前存储契约只允许不复权行情")
    if frame["volume_unit"].nunique() != 1:
        raise DataValidationError("同一行情文件不能混合成交量单位")
    if raw_snapshot and frame["source"].nunique() != 1:
        raise DataValidationError("原始快照必须来自单一来源")
    values = frame["ingested_at"]
    if values.map(lambda value: isinstance(value, Number)).any():
        raise DataValidationError("采集时间不能使用数值或布尔值")
    try:
        timestamps = pd.to_datetime(values, errors="coerce", format="mixed", utc=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataValidationError("采集时间无法解析") from exc
    if timestamps.isna().any():
        raise DataValidationError("采集时间存在空值或无法解析的值")


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
    _assert_storage_metadata(frame, raw_snapshot=True)
    source = _safe_path_component(str(frame["source"].iloc[0]), label="source")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = _contained_path(paths.raw, source, _safe_symbol(symbol))
    directory.mkdir(parents=True, exist_ok=True)
    output = _contained_path(directory, f"{timestamp}_{uuid.uuid4().hex}.parquet")
    write_parquet_immutable(frame, output)
    return output


def save_curated(
    frame: pd.DataFrame, paths: ProjectPaths, *, preferred_source: str | None = None,
) -> Path:
    with market_data_lock(paths.data):
        assert_market_data_writable(paths)
        return _save_curated(frame, paths, preferred_source=preferred_source)


def _save_curated(
    frame: pd.DataFrame, paths: ProjectPaths, *, preferred_source: str | None = None,
) -> Path:
    assert_valid_daily_prices(frame)
    symbol = assert_data_identity(frame)
    _assert_storage_metadata(frame)
    if preferred_source is not None and (
        preferred_source != "tencent" or not frame["source"].eq(preferred_source).all()
    ):
        raise ValueError("显式优先来源必须为 tencent，且与输入行情来源一致")
    output = _contained_path(paths.curated, f"{_safe_symbol(symbol)}.parquet")
    if output.exists():
        previous = pd.read_parquet(output)
        assert_data_identity(previous, filename=output.name, expected_symbol=symbol)
        assert_valid_daily_prices(previous)
        _assert_storage_metadata(previous)
        combined = pd.concat([previous, frame], ignore_index=True)
        combined["trade_date"] = pd.to_datetime(combined["trade_date"])
        # A temporary fallback response must not overwrite an existing preferred-source row.
        source_priority = {"akshare": 20, "baostock": 10}
        if preferred_source is not None:
            source_priority[preferred_source] = 30
        combined["_source_priority"] = combined["source"].map(source_priority).fillna(0)
        combined = combined.sort_values(
            ["trade_date", "_source_priority", "ingested_at"]
        )
        frame = combined.drop_duplicates(["symbol", "trade_date"], keep="last")
        frame = frame.drop(columns="_source_priority")
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    assert_valid_daily_prices(frame)
    assert_data_identity(frame, filename=output.name, expected_symbol=symbol)
    _assert_storage_metadata(frame)
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
    # Validate every complete file before opening the destination, while holding
    # the project lock. Missing columns must not be filled by union_by_name.
    with duckdb.connect(":memory:") as connection:
        for path in parquet_files:
            identity = connection.execute(
                "SELECT symbol FROM read_parquet(?)", [path.as_posix()]
            ).fetchdf()
            assert_data_identity(identity, filename=path.name)
            frame = connection.execute("SELECT * FROM read_parquet(?)", [path.as_posix()]).fetchdf()
            assert_valid_daily_prices(frame)
            _assert_storage_metadata(frame)
    with duckdb.connect(str(paths.database)) as connection:
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                f"""
                CREATE OR REPLACE TABLE daily_prices AS
                SELECT * FROM read_parquet([{file_list}], union_by_name = true)
                ORDER BY symbol, trade_date
                """
            )
            result = connection.execute("SELECT COUNT(*) FROM daily_prices").fetchone()
            if result is None:
                raise RuntimeError("行情入库计数缺失")
            count = int(result[0])
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return count


def read_curated(symbol: str, paths: ProjectPaths) -> pd.DataFrame:
    with market_data_lock(paths.data):
        assert_market_data_readable(paths)
        return _read_curated(symbol, paths)


def _read_curated(symbol: str, paths: ProjectPaths) -> pd.DataFrame:
    path = _contained_path(paths.curated, f"{_safe_symbol(symbol)}.parquet")
    if not path.exists():
        raise FileNotFoundError(f"尚未找到 {symbol} 的整理数据，请先运行数据更新")
    frame = read_curated_parquet(path)
    assert_data_identity(frame, filename=path.name, expected_symbol=symbol)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.sort_values("trade_date").reset_index(drop=True)
