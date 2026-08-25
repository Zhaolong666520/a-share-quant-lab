from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import BacktestMetrics, BacktestResult, calculate_metrics, run_backtest
from finance_lab.experiment import data_fingerprint

STRATEGY_ORDER = ("buy_hold", "sma", "momentum")
STRATEGY_LABELS = {
    "buy_hold": "买入持有",
    "sma": "双均线",
    "momentum": "时间序列动量",
}


@dataclass(frozen=True)
class StrategyComparisonScenario:
    strategy: str
    label: str
    parameters: dict[str, int]
    full_result: BacktestResult
    development_metrics: BacktestMetrics
    out_of_sample_metrics: BacktestMetrics
    out_of_sample_frame: pd.DataFrame

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "parameters": self.parameters,
            "development_metrics": self.development_metrics.to_dict(),
            "out_of_sample_metrics": self.out_of_sample_metrics.to_dict(),
        }


@dataclass(frozen=True)
class StrategyComparisonResult:
    experiment_id: str
    data_fingerprint: str
    dataset_id: str | None
    curated_file_sha256: str | None
    data_health_status: str | None
    manifest_health_status: str | None
    manifest_generated_at: str | None
    manifest_as_of_date: date | None
    business_days_stale: int | None
    data_warning_codes: tuple[str, ...]
    upstream_errors: dict[str, str]
    data_start_date: date | None
    data_end_date: date | None
    data_sources: dict[str, int]
    data_adjustments: tuple[str, ...]
    data_volume_units: tuple[str, ...]
    symbol: str
    split_date: date
    cost_bps: float
    short_window: int
    long_window: int
    momentum_lookback: int
    development_start: date
    development_end: date
    out_of_sample_start: date
    out_of_sample_end: date
    scenarios: tuple[StrategyComparisonScenario, ...]
    benchmark_metrics: BacktestMetrics

    def settings_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "engine_version": __version__,
            "data_fingerprint_sha256": self.data_fingerprint,
            "dataset_id": self.dataset_id,
            "curated_file_sha256": self.curated_file_sha256,
            "data_health_status": self.data_health_status,
            "manifest_health_status": self.manifest_health_status,
            "manifest_generated_at": self.manifest_generated_at,
            "manifest_as_of_date": (
                self.manifest_as_of_date.isoformat() if self.manifest_as_of_date else None
            ),
            "business_days_stale": self.business_days_stale,
            "data_warning_codes": list(self.data_warning_codes),
            "upstream_errors": self.upstream_errors,
            "data_start_date": (
                self.data_start_date.isoformat() if self.data_start_date else None
            ),
            "data_end_date": self.data_end_date.isoformat() if self.data_end_date else None,
            "data_sources": self.data_sources,
            "data_adjustments": list(self.data_adjustments),
            "data_volume_units": list(self.data_volume_units),
            "symbol": self.symbol,
            "split_date": self.split_date.isoformat(),
            "cost_bps": self.cost_bps,
            "strategy_order": list(STRATEGY_ORDER),
            "sma": {"short_window": self.short_window, "long_window": self.long_window},
            "momentum": {"lookback": self.momentum_lookback},
        }


def _period_metrics(frame: pd.DataFrame, return_column: str = "net_return") -> BacktestMetrics:
    returns = frame[return_column].astype(float)
    equity = (1.0 + returns).cumprod()
    return calculate_metrics(returns, equity, frame["position"], frame["turnover"])


def _benchmark_metrics(frame: pd.DataFrame) -> BacktestMetrics:
    returns = frame["benchmark_return"].astype(float)
    equity = (1.0 + returns).cumprod()
    return calculate_metrics(
        returns,
        equity,
        frame["benchmark_position"],
        frame["benchmark_turnover"],
    )


def _validate_warmup(
    result: BacktestResult,
    first_oos_row: pd.Series,
    indicator_column: str,
    strategy_label: str,
) -> None:
    signal_date = pd.Timestamp(first_oos_row["signal_date"])
    signal_rows = result.frame[result.frame["trade_date"] == signal_date]
    if signal_rows.empty or not math.isfinite(float(signal_rows.iloc[0][indicator_column])):
        raise ValueError(f"{strategy_label}在首个样本外交易日前没有完成指标预热")


def _experiment_id(
    split_date: date,
    short_window: int,
    long_window: int,
    momentum_lookback: int,
    cost_bps: float,
    lineage_fingerprint: str,
) -> str:
    settings = json.dumps(
        {
            "split_date": split_date.isoformat(),
            "short_window": short_window,
            "long_window": long_window,
            "momentum_lookback": momentum_lookback,
            "cost_bps": cost_bps,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    config_hash = hashlib.sha256(settings.encode("utf-8")).hexdigest()[:12]
    version_token = __version__.replace(".", "")
    return (
        f"v{version_token}_compare_{split_date:%Y%m%d}_s{short_window}_l{long_window}_"
        f"m{momentum_lookback}_g{config_hash}_{lineage_fingerprint[:16]}"
    )


def run_strategy_comparison(
    prices: pd.DataFrame,
    split_date: date,
    short_window: int = 20,
    long_window: int = 60,
    momentum_lookback: int = 120,
    cost_bps: float = 5.0,
    min_period_observations: int = 120,
    dataset_id: str | None = None,
    curated_file_sha256: str | None = None,
    data_health_status: str | None = None,
    manifest_health_status: str | None = None,
    manifest_generated_at: str | None = None,
    manifest_as_of_date: date | None = None,
    business_days_stale: int | None = None,
    data_warning_codes: tuple[str, ...] = (),
    upstream_errors: dict[str, str] | None = None,
    data_start_date: date | None = None,
    data_end_date: date | None = None,
    data_sources: dict[str, int] | None = None,
    data_adjustments: tuple[str, ...] = (),
    data_volume_units: tuple[str, ...] = (),
) -> StrategyComparisonResult:
    """Compare a fixed strategy set on identical development and OOS boundaries."""
    if min_period_observations < 2:
        raise ValueError("min_period_observations must be at least 2")
    if curated_file_sha256 is not None and not re.fullmatch(
        r"[0-9a-fA-F]{64}", curated_file_sha256
    ):
        raise ValueError("curated_file_sha256 必须是64位十六进制SHA-256")

    results = {
        strategy: run_backtest(
            prices,
            strategy=strategy,
            short_window=short_window,
            long_window=long_window,
            momentum_lookback=momentum_lookback,
            cost_bps=cost_bps,
        )
        for strategy in STRATEGY_ORDER
    }
    split_timestamp = pd.Timestamp(split_date)
    reference_frame = results[STRATEGY_ORDER[0]].frame
    development_mask = reference_frame["return_end_date"] < split_timestamp
    out_of_sample_mask = reference_frame["trade_date"] >= split_timestamp
    if int(development_mask.sum()) < min_period_observations:
        raise ValueError("开发期数据不足，请把切分日期向后调整")
    if int(out_of_sample_mask.sum()) < min_period_observations:
        raise ValueError("样本外数据不足，请把切分日期向前调整")

    first_oos_row = reference_frame.loc[out_of_sample_mask].iloc[0]
    _validate_warmup(results["sma"], first_oos_row, "sma_long", "双均线")
    _validate_warmup(
        results["momentum"],
        first_oos_row,
        "momentum_return",
        "时间序列动量",
    )

    reference_trade_dates = reference_frame.loc[
        out_of_sample_mask, "trade_date"
    ].reset_index(drop=True)
    reference_benchmark = reference_frame.loc[
        out_of_sample_mask, "benchmark_return"
    ].reset_index(drop=True)
    scenarios: list[StrategyComparisonScenario] = []
    for strategy in STRATEGY_ORDER:
        result = results[strategy]
        development = result.frame[result.frame["return_end_date"] < split_timestamp].copy()
        out_of_sample = result.frame[result.frame["trade_date"] >= split_timestamp].copy()
        if not out_of_sample["trade_date"].reset_index(drop=True).equals(reference_trade_dates):
            raise RuntimeError("策略样本外日期边界不一致")
        if not np.allclose(
            out_of_sample["benchmark_return"].to_numpy(),
            reference_benchmark.to_numpy(),
            rtol=0.0,
            atol=0.0,
            equal_nan=True,
        ):
            raise RuntimeError("策略使用的买入持有基准不一致")
        out_of_sample["period_equity"] = (1.0 + out_of_sample["net_return"]).cumprod()
        parameters = (
            {"short_window": short_window, "long_window": long_window}
            if strategy == "sma"
            else {"lookback": momentum_lookback}
            if strategy == "momentum"
            else {}
        )
        scenarios.append(
            StrategyComparisonScenario(
                strategy=strategy,
                label=STRATEGY_LABELS[strategy],
                parameters=parameters,
                full_result=result,
                development_metrics=_period_metrics(development),
                out_of_sample_metrics=_period_metrics(out_of_sample),
                out_of_sample_frame=out_of_sample,
            )
        )

    benchmark_frame = reference_frame.loc[out_of_sample_mask].copy()
    fingerprint = data_fingerprint(prices)
    lineage_fingerprint = curated_file_sha256 or fingerprint
    return StrategyComparisonResult(
        experiment_id=_experiment_id(
            split_date,
            short_window,
            long_window,
            momentum_lookback,
            cost_bps,
            lineage_fingerprint,
        ),
        data_fingerprint=fingerprint,
        dataset_id=dataset_id,
        curated_file_sha256=curated_file_sha256,
        data_health_status=data_health_status,
        manifest_health_status=manifest_health_status,
        manifest_generated_at=manifest_generated_at,
        manifest_as_of_date=manifest_as_of_date,
        business_days_stale=business_days_stale,
        data_warning_codes=data_warning_codes,
        upstream_errors=upstream_errors or {},
        data_start_date=data_start_date,
        data_end_date=data_end_date,
        data_sources=data_sources or {},
        data_adjustments=data_adjustments,
        data_volume_units=data_volume_units,
        symbol=str(prices["symbol"].iloc[0]),
        split_date=split_date,
        cost_bps=cost_bps,
        short_window=short_window,
        long_window=long_window,
        momentum_lookback=momentum_lookback,
        development_start=pd.Timestamp(
            reference_frame.loc[development_mask, "return_end_date"].iloc[0]
        ).date(),
        development_end=pd.Timestamp(
            reference_frame.loc[development_mask, "return_end_date"].iloc[-1]
        ).date(),
        out_of_sample_start=pd.Timestamp(reference_trade_dates.iloc[0]).date(),
        out_of_sample_end=pd.Timestamp(
            benchmark_frame["return_end_date"].iloc[-1]
        ).date(),
        scenarios=tuple(scenarios),
        benchmark_metrics=_benchmark_metrics(benchmark_frame),
    )
