from __future__ import annotations

import math

import pandas as pd
import pytest

from finance_lab.paper_observation import (
    OBSERVATION_MILESTONES,
    build_forward_observation_history,
    calculate_observation_progress,
)


def _daily(rows: int = 4) -> pd.DataFrame:
    dates = pd.date_range("2026-08-24", periods=rows, freq="B")
    closes = [10.0 + index for index in range(rows)]
    records: list[dict[str, object]] = []
    for account_id, equities in (
        ("study-sma", [100_000.0 + 500.0 * index for index in range(rows)]),
        ("study-momentum", [100_000.0 - 250.0 * index for index in range(rows)]),
    ):
        for trade_date, close, equity in zip(dates, closes, equities, strict=True):
            records.append(
                {
                    "account_id": account_id,
                    "trade_date": trade_date,
                    "close_price": close,
                    "equity": equity,
                }
            )
    return pd.DataFrame(records)


@pytest.mark.parametrize(
    ("valuation_rows", "reached", "next_target", "remaining", "stage"),
    [
        (1, (), 20, 20, "initial_observation"),
        (21, (20,), 60, 40, "twenty_day_observation"),
        (61, (20, 60), 120, 60, "sixty_day_observation"),
        (121, (20, 60, 120), 252, 132, "one_hundred_twenty_day_observation"),
        (253, (20, 60, 120, 252), None, None, "one_year_observation"),
    ],
)
def test_observation_progress_uses_fixed_return_milestones(
    valuation_rows: int,
    reached: tuple[int, ...],
    next_target: int | None,
    remaining: int | None,
    stage: str,
) -> None:
    progress = calculate_observation_progress(_daily(valuation_rows))

    assert OBSERVATION_MILESTONES == (20, 60, 120, 252)
    assert progress.valuation_observations == valuation_rows
    assert progress.return_observations == valuation_rows - 1
    assert progress.reached_milestones == reached
    assert progress.next_milestone == next_target
    assert progress.observations_to_next_milestone == remaining
    assert progress.stage == stage
    assert progress.minimum_evidence_observations == 60
    assert progress.minimum_window_reached == (valuation_rows - 1 >= 60)
    assert progress.authorizes_real_money is False


def test_observation_progress_reports_capped_minimum_window_completion() -> None:
    early = calculate_observation_progress(_daily(31))
    mature = calculate_observation_progress(_daily(80))

    assert early.minimum_window_completion == pytest.approx(0.5)
    assert mature.minimum_window_completion == pytest.approx(1.0)


def test_observation_history_records_prefix_only_account_and_benchmark_returns() -> None:
    daily = _daily()

    history = build_forward_observation_history(
        daily,
        initial_cash_by_account={"study-sma": 100_000.0, "study-momentum": 100_000.0},
    )

    assert list(history.columns) == [
        "account_id",
        "trade_date",
        "valuation_observations",
        "return_observations",
        "equity",
        "account_total_return",
        "cash_benchmark_return",
        "return_difference_vs_cash",
        "asset_price_benchmark_return",
        "return_difference_vs_asset_price",
        "minimum_window_reached",
    ]
    assert len(history) == 8
    first = history.loc[history["account_id"] == "study-sma"].iloc[0]
    last = history.loc[history["account_id"] == "study-sma"].iloc[-1]
    assert first["trade_date"] == "2026-08-24"
    assert first["valuation_observations"] == 1
    assert first["return_observations"] == 0
    assert first["account_total_return"] == pytest.approx(0.0)
    assert last["valuation_observations"] == 4
    assert last["account_total_return"] == pytest.approx(0.015)
    assert last["asset_price_benchmark_return"] == pytest.approx(0.3)
    assert last["return_difference_vs_asset_price"] == pytest.approx(-0.285)
    assert history["cash_benchmark_return"].eq(0.0).all()
    assert not history["minimum_window_reached"].any()


def test_observation_history_does_not_rewrite_earlier_rows_from_future_values() -> None:
    original = _daily()
    mutated = original.copy()
    final_date = mutated["trade_date"].max()
    mutated.loc[mutated["trade_date"] == final_date, "equity"] *= 1.7
    mutated.loc[mutated["trade_date"] == final_date, "close_price"] *= 1.4

    first = build_forward_observation_history(
        original,
        initial_cash_by_account={"study-sma": 100_000.0, "study-momentum": 100_000.0},
    )
    second = build_forward_observation_history(
        mutated,
        initial_cash_by_account={"study-sma": 100_000.0, "study-momentum": 100_000.0},
    )

    pd.testing.assert_frame_equal(
        first.loc[first["trade_date"] != "2026-08-27"].reset_index(drop=True),
        second.loc[second["trade_date"] != "2026-08-27"].reset_index(drop=True),
    )


def test_observation_history_rejects_accounts_with_different_close_paths() -> None:
    daily = _daily()
    daily.loc[
        (daily["account_id"] == "study-momentum")
        & (daily["trade_date"] == daily["trade_date"].max()),
        "close_price",
    ] = 99.0

    with pytest.raises(ValueError, match="完全相同"):
        build_forward_observation_history(
            daily,
            initial_cash_by_account={
                "study-sma": 100_000.0,
                "study-momentum": 100_000.0,
            },
        )


@pytest.mark.parametrize(
    ("cash_map", "message"),
    [
        ({"study-sma": 100_000.0}, "初始资金账户"),
        (
            {"study-sma": 100_000.0, "study-momentum": math.inf},
            "初始资金必须是有限正数",
        ),
    ],
)
def test_observation_history_rejects_invalid_initial_cash_mapping(
    cash_map: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_forward_observation_history(_daily(), initial_cash_by_account=cash_map)


def test_observation_history_rejects_initial_equity_mismatch() -> None:
    daily = _daily()
    daily.loc[daily["account_id"] == "study-sma", "equity"] += 1.0

    with pytest.raises(ValueError, match="首日权益"):
        build_forward_observation_history(
            daily,
            initial_cash_by_account={
                "study-sma": 100_000.0,
                "study-momentum": 100_000.0,
            },
        )


@pytest.mark.parametrize("minimum", [True, 1, 60.5])
def test_observation_progress_rejects_invalid_minimum_window(minimum: object) -> None:
    with pytest.raises(ValueError, match="最小观察窗"):
        calculate_observation_progress(
            _daily(),
            minimum_evidence_observations=minimum,  # type: ignore[arg-type]
        )
