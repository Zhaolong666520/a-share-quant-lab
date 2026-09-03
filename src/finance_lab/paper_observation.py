"""Deterministic progress history for committed paper-trading valuations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from finance_lab.paper_benchmark import calculate_shared_price_benchmark

OBSERVATION_MILESTONES = (20, 60, 120, 252)
OBSERVATION_HISTORY_COLUMNS = (
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
)


@dataclass(frozen=True)
class PaperObservationProgress:
    start_date: str
    end_date: str
    valuation_observations: int
    return_observations: int
    milestone_targets: tuple[int, ...]
    reached_milestones: tuple[int, ...]
    next_milestone: int | None
    observations_to_next_milestone: int | None
    minimum_evidence_observations: int
    minimum_window_reached: bool
    minimum_window_completion: float
    stage: str
    scope: str
    authorizes_real_money: bool


def calculate_observation_progress(
    daily: pd.DataFrame,
    *,
    minimum_evidence_observations: int = 60,
) -> PaperObservationProgress:
    """Summarize shared forward sample growth without judging performance."""
    _validate_minimum_window(minimum_evidence_observations)
    benchmark = calculate_shared_price_benchmark(daily)
    observations = benchmark.return_observations
    reached = tuple(target for target in OBSERVATION_MILESTONES if target <= observations)
    next_milestone = next(
        (target for target in OBSERVATION_MILESTONES if target > observations),
        None,
    )
    return PaperObservationProgress(
        start_date=benchmark.start_date,
        end_date=benchmark.end_date,
        valuation_observations=benchmark.valuation_observations,
        return_observations=observations,
        milestone_targets=OBSERVATION_MILESTONES,
        reached_milestones=reached,
        next_milestone=next_milestone,
        observations_to_next_milestone=(
            next_milestone - observations if next_milestone is not None else None
        ),
        minimum_evidence_observations=minimum_evidence_observations,
        minimum_window_reached=observations >= minimum_evidence_observations,
        minimum_window_completion=min(
            observations / minimum_evidence_observations,
            1.0,
        ),
        stage=_observation_stage(observations),
        scope="sample_progress_only",
        authorizes_real_money=False,
    )


def build_forward_observation_history(
    daily: pd.DataFrame,
    *,
    initial_cash_by_account: Mapping[str, float],
    minimum_evidence_observations: int = 60,
) -> pd.DataFrame:
    """Build prefix-only cumulative comparisons from committed valuations."""
    _validate_minimum_window(minimum_evidence_observations)
    calculate_shared_price_benchmark(daily)
    if "equity" not in daily.columns:
        raise ValueError("前向观察历史缺少字段：equity")
    frame = daily.copy()
    frame["account_id"] = frame["account_id"].astype(str)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="raise")
    account_ids = set(frame["account_id"])
    if set(initial_cash_by_account) != account_ids:
        raise ValueError("初始资金账户必须与估值账户完全一致")

    initial_cash: dict[str, float] = {}
    for account_id, value in initial_cash_by_account.items():
        initial_cash[account_id] = _positive_number(value, "初始资金必须是有限正数")

    rows: list[dict[str, object]] = []
    for account_id in sorted(account_ids):
        account = frame.loc[frame["account_id"] == account_id].sort_values("trade_date")
        equities = [
            _positive_number(value, "账户权益必须是有限正数")
            for value in account["equity"]
        ]
        cash = initial_cash[account_id]
        if not math.isclose(equities[0], cash, rel_tol=0.0, abs_tol=1e-8):
            raise ValueError("首日权益必须等于初始资金")
        start_close = float(account["close_price"].iloc[0])
        for index, ((_, valuation), equity) in enumerate(
            zip(account.iterrows(), equities, strict=True)
        ):
            account_return = equity / cash - 1.0
            asset_return = float(valuation["close_price"]) / start_close - 1.0
            return_observations = index
            rows.append(
                {
                    "account_id": account_id,
                    "trade_date": valuation["trade_date"].date().isoformat(),
                    "valuation_observations": index + 1,
                    "return_observations": return_observations,
                    "equity": equity,
                    "account_total_return": account_return,
                    "cash_benchmark_return": 0.0,
                    "return_difference_vs_cash": account_return,
                    "asset_price_benchmark_return": asset_return,
                    "return_difference_vs_asset_price": account_return - asset_return,
                    "minimum_window_reached": (
                        return_observations >= minimum_evidence_observations
                    ),
                }
            )
    history = pd.DataFrame(rows, columns=OBSERVATION_HISTORY_COLUMNS)
    return history.sort_values(["trade_date", "account_id"]).reset_index(drop=True)


def _observation_stage(return_observations: int) -> str:
    if return_observations < 20:
        return "initial_observation"
    if return_observations < 60:
        return "twenty_day_observation"
    if return_observations < 120:
        return "sixty_day_observation"
    if return_observations < 252:
        return "one_hundred_twenty_day_observation"
    return "one_year_observation"


def _validate_minimum_window(value: int) -> None:
    if type(value) is not int or value < 2:
        raise ValueError("最小观察窗必须是至少为 2 的整数")


def _positive_number(value: object, message: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(message)
    return number
