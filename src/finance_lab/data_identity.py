from __future__ import annotations

import re

import pandas as pd

from finance_lab.validation import DataValidationError, ValidationIssue

_CANONICAL_SYMBOL = re.compile(r"[a-z][a-z0-9]{0,15}\.[0-9]{6}")
_SYMBOL_FORMAT_ERROR = "标的格式不规范，不接受路径或别名；应为小写命名空间与六位数字，如 sh.510300"


def _is_canonical_symbol(value: object) -> bool:
    return isinstance(value, str) and _CANONICAL_SYMBOL.fullmatch(value) is not None


def canonical_symbol_stem(symbol: str) -> str:
    """Map a canonical symbol to its filename stem without accepting aliases."""
    if not _is_canonical_symbol(symbol):
        raise DataValidationError(_SYMBOL_FORMAT_ERROR)
    return symbol.replace(".", "_")


def validate_data_identity(
    frame: pd.DataFrame,
    *,
    filename: str | None = None,
    expected_symbol: str | None = None,
) -> list[ValidationIssue]:
    """Validate one file's identity without coercion, renaming, or file writes."""
    if "symbol" not in frame.columns or frame.empty:
        return [
            ValidationIssue("error", "missing_symbol", "没有可核验的标的：缺少 symbol 列或数据为空")
        ]
    if not frame["symbol"].map(_is_canonical_symbol).all():
        return [ValidationIssue("error", "invalid_symbol", _SYMBOL_FORMAT_ERROR)]
    symbols = frame["symbol"].unique()
    if len(symbols) != 1:
        return [ValidationIssue("error", "mixed_symbols", "同一数据文件必须只包含一个标的")]
    symbol = str(symbols[0])
    issues: list[ValidationIssue] = []
    canonical_filename = f"{canonical_symbol_stem(symbol)}.parquet"
    if filename is not None and filename != canonical_filename:
        issues.append(
            ValidationIssue(
                "error",
                "symbol_filename_mismatch",
                f"标的 {symbol} 与文件名 {filename} 不一致，预期为 {canonical_filename}",
            )
        )
    if expected_symbol is not None and symbol != expected_symbol:
        issues.append(
            ValidationIssue(
                "error",
                "unexpected_symbol",
                f"数据标的 {symbol} 与请求标的 {expected_symbol} 不一致",
            )
        )
    return issues


def assert_data_identity(
    frame: pd.DataFrame,
    *,
    filename: str | None = None,
    expected_symbol: str | None = None,
) -> str:
    issues = validate_data_identity(frame, filename=filename, expected_symbol=expected_symbol)
    if issues:
        raise DataValidationError("; ".join(issue.message for issue in issues))
    return str(frame["symbol"].iloc[0])
