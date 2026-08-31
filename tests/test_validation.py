from __future__ import annotations

import numpy as np
import pytest

from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.validation import compare_sources, validate_daily_prices


def test_synthetic_data_passes_validation() -> None:
    frame = make_synthetic_daily_prices(periods=150)
    assert not [issue for issue in validate_daily_prices(frame) if issue.severity == "error"]


def test_invalid_high_is_detected() -> None:
    frame = make_synthetic_daily_prices(periods=150)
    frame.loc[5, "high"] = frame.loc[5, "low"] - 1
    codes = {issue.code for issue in validate_daily_prices(frame)}
    assert "invalid_high" in codes


def test_duplicate_key_is_detected() -> None:
    frame = make_synthetic_daily_prices(periods=150)
    frame.loc[1, "trade_date"] = frame.loc[0, "trade_date"]
    codes = {issue.code for issue in validate_daily_prices(frame)}
    assert "duplicate_key" in codes


@pytest.mark.parametrize(
    ("column", "expected_code"),
    [
        ("open", "non_finite_price"),
        ("high", "non_finite_price"),
        ("low", "non_finite_price"),
        ("close", "non_finite_price"),
        ("volume", "invalid_volume"),
        ("amount", "invalid_amount"),
    ],
)
def test_non_finite_numeric_market_data_is_rejected(
    column: str,
    expected_code: str,
) -> None:
    frame = make_synthetic_daily_prices(periods=150)
    frame[column] = frame[column].astype(float)
    frame.loc[5, column] = np.inf
    codes = {issue.code for issue in validate_daily_prices(frame)}

    assert expected_code in codes


def test_source_comparison_warns_when_date_coverage_is_low() -> None:
    primary = make_synthetic_daily_prices(periods=150)
    secondary = primary.tail(20).copy()
    comparison = compare_sources(primary, secondary)
    assert comparison["status"] == "warning"
    coverage_ratio = comparison["coverage_ratio"]
    assert isinstance(coverage_ratio, float)
    assert coverage_ratio < 0.95
