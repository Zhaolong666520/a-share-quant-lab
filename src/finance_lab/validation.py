from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from finance_lab.sources import CANONICAL_COLUMNS


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class DataValidationError(ValueError):
    pass


def validate_daily_prices(frame: pd.DataFrame) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    missing = [column for column in CANONICAL_COLUMNS if column not in frame.columns]
    if missing:
        return [ValidationIssue("error", "missing_columns", f"缺少字段: {missing}")]
    if frame.empty:
        return [ValidationIssue("error", "empty_data", "数据为空")]

    if frame.duplicated(["symbol", "trade_date"]).any():
        issues.append(ValidationIssue("error", "duplicate_key", "存在重复的标的与交易日"))
    if not frame["trade_date"].is_monotonic_increasing:
        issues.append(ValidationIssue("error", "date_order", "交易日期没有升序排列"))
    if frame["trade_date"].isna().any():
        issues.append(ValidationIssue("error", "invalid_date", "存在无法解析的交易日期"))

    price_columns = ["open", "high", "low", "close"]
    prices = frame[price_columns].apply(pd.to_numeric, errors="coerce")
    if prices.isna().any().any():
        issues.append(ValidationIssue("error", "invalid_price", "价格字段存在空值或非数字"))
    if not np.isfinite(prices.to_numpy(dtype=float)).all():
        issues.append(ValidationIssue("error", "non_finite_price", "价格字段存在非有限数字"))
    if (prices <= 0).any().any():
        issues.append(ValidationIssue("error", "non_positive_price", "价格必须大于零"))

    max_ohlc = prices[["open", "close", "low"]].max(axis=1)
    min_ohlc = prices[["open", "close", "high"]].min(axis=1)
    if (prices["high"] + 1e-9 < max_ohlc).any():
        issues.append(ValidationIssue("error", "invalid_high", "最高价低于其他价格字段"))
    if (prices["low"] - 1e-9 > min_ohlc).any():
        issues.append(ValidationIssue("error", "invalid_low", "最低价高于其他价格字段"))

    volume = pd.to_numeric(frame["volume"], errors="coerce")
    if (
        volume.isna().any()
        or not np.isfinite(volume.to_numpy(dtype=float)).all()
        or (volume < 0).any()
    ):
        issues.append(ValidationIssue("error", "invalid_volume", "成交量存在空值、非有限值或负数"))

    amount = pd.to_numeric(frame["amount"], errors="coerce")
    if (
        amount.isna().any()
        or not np.isfinite(amount.to_numpy(dtype=float)).all()
        or (amount < 0).any()
    ):
        issues.append(ValidationIssue("error", "invalid_amount", "成交额存在空值、非有限值或负数"))

    gaps = frame["trade_date"].sort_values().diff().dt.days.dropna()
    if not gaps.empty and int(gaps.max()) > 15:
        issues.append(ValidationIssue("warning", "large_date_gap", "相邻交易日间隔超过15天"))
    return issues


def assert_valid_daily_prices(frame: pd.DataFrame) -> list[ValidationIssue]:
    issues = validate_daily_prices(frame)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        messages = "; ".join(issue.message for issue in errors)
        raise DataValidationError(messages)
    return issues


def compare_sources(primary: pd.DataFrame, secondary: pd.DataFrame) -> dict[str, object]:
    left = primary[["trade_date", "close"]].rename(columns={"close": "close_primary"})
    right = secondary[["trade_date", "close"]].rename(columns={"close": "close_secondary"})
    merged = left.merge(right, on="trade_date", how="outer", indicator=True)
    common = merged[merged["_merge"] == "both"].copy()
    if common.empty:
        return {
            "status": "warning",
            "message": "两个来源没有重叠交易日",
            "common_rows": 0,
            "primary_only": int((merged["_merge"] == "left_only").sum()),
            "secondary_only": int((merged["_merge"] == "right_only").sum()),
        }

    denominator = common[["close_primary", "close_secondary"]].abs().max(axis=1)
    relative = (common["close_primary"] - common["close_secondary"]).abs() / denominator
    p95 = float(np.nanpercentile(relative, 95))
    coverage_ratio = float(len(common) / max(len(primary), len(secondary)))
    prices_match = p95 <= 0.005
    coverage_is_sufficient = coverage_ratio >= 0.95
    status = "pass" if prices_match and coverage_is_sufficient else "warning"
    if not prices_match:
        message = "来源价格差异偏大，请人工检查"
    elif not coverage_is_sufficient:
        message = "重叠日期价格一致，但第二来源覆盖不足95%"
    else:
        message = "日期覆盖充分，且收盘价95分位相对差异不超过0.5%"
    return {
        "status": status,
        "message": message,
        "common_rows": int(len(common)),
        "primary_only": int((merged["_merge"] == "left_only").sum()),
        "secondary_only": int((merged["_merge"] == "right_only").sum()),
        "coverage_ratio": coverage_ratio,
        "median_close_relative_diff": float(relative.median()),
        "p95_close_relative_diff": p95,
        "max_close_relative_diff": float(relative.max()),
    }
