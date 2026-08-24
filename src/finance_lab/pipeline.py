from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from finance_lab.backtest import run_backtest
from finance_lab.config import Instrument, ProjectPaths, get_paths, load_instruments
from finance_lab.cost_sensitivity import run_cost_sensitivity
from finance_lab.cost_sensitivity_report import write_cost_sensitivity_report
from finance_lab.data_manifest import (
    file_sha256,
    generate_dataset_manifest,
    write_dataset_manifest_report,
)
from finance_lab.execution_feasibility import run_execution_feasibility
from finance_lab.execution_feasibility_report import write_execution_feasibility_report
from finance_lab.experiment import run_split_experiment
from finance_lab.experiment_report import write_split_experiment_report
from finance_lab.ledger import LedgerConfig, LedgerDataHealth, run_account_ledger
from finance_lab.ledger_report import write_account_ledger_report
from finance_lab.parameter_sensitivity import run_parameter_sensitivity
from finance_lab.parameter_sensitivity_report import write_parameter_sensitivity_report
from finance_lab.report import write_backtest_report
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.sources import DataSourceError, fetch_akshare, fetch_baostock
from finance_lab.storage import (
    read_curated,
    save_curated,
    save_raw_snapshot,
    sync_duckdb,
)
from finance_lab.validation import (
    assert_valid_daily_prices,
    compare_sources,
    validate_daily_prices,
)
from finance_lab.walk_forward import run_walk_forward
from finance_lab.walk_forward_report import write_walk_forward_report


@dataclass
class UpdateRecord:
    symbol: str
    name: str
    rows: int = 0
    primary_source: str | None = None
    raw_files: list[str] = field(default_factory=list)
    curated_file: str | None = None
    source_errors: dict[str, str] = field(default_factory=dict)
    source_comparison: dict[str, object] | None = None


def _fetch_instrument(
    instrument: Instrument,
    start: date,
    end: date,
    paths: ProjectPaths,
) -> UpdateRecord:
    record = UpdateRecord(symbol=instrument.symbol, name=instrument.name)
    frames: dict[str, pd.DataFrame] = {}
    fetchers = {"akshare": fetch_akshare, "baostock": fetch_baostock}
    for source, fetcher in fetchers.items():
        try:
            frame = fetcher(instrument, start, end)
            assert_valid_daily_prices(frame)
            raw_path = save_raw_snapshot(frame, paths)
            frames[source] = frame
            record.raw_files.append(str(raw_path.relative_to(paths.root)))
        except (DataSourceError, ValueError) as exc:
            record.source_errors[source] = str(exc)

    if not frames:
        return record

    primary_source = "akshare" if "akshare" in frames else next(iter(frames))
    primary = frames[primary_source]
    curated_path = save_curated(primary, paths)
    record.primary_source = primary_source
    record.rows = int(len(primary))
    record.curated_file = str(curated_path.relative_to(paths.root))
    if len(frames) >= 2:
        secondary_source = next(source for source in frames if source != primary_source)
        record.source_comparison = compare_sources(primary, frames[secondary_source])
    return record


def update_market_data(
    start: date,
    end: date,
    root: Path | None = None,
) -> dict[str, Any]:
    if start >= end:
        raise ValueError("开始日期必须早于结束日期")
    paths = get_paths(root)
    records = [
        _fetch_instrument(instrument, start, end, paths)
        for instrument in load_instruments(paths.root)
    ]
    successful = [record for record in records if record.rows > 0]
    database_rows = sync_duckdb(paths) if successful else 0
    payload: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "successful_instruments": len(successful),
        "database_rows": database_rows,
        "records": [asdict(record) for record in records],
    }
    output = paths.outputs / "update_summary.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def create_demo_report(root: Path | None = None) -> tuple[Path, dict[str, float | int]]:
    paths = get_paths(root)
    prices = make_synthetic_daily_prices()
    assert_valid_daily_prices(prices)
    result = run_backtest(prices, strategy="sma", short_window=20, long_window=60)
    html_path, _, _ = write_backtest_report(
        result,
        paths,
        title="Finance Lab 离线演示报告（合成数据）",
    )
    return html_path, result.metrics.to_dict()


def data_health(
    root: Path | None = None,
    as_of_date: date | None = None,
    stale_after_business_days: int = 3,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
    manifest = generate_dataset_manifest(
        paths,
        as_of_date=as_of_date,
        stale_after_business_days=stale_after_business_days,
    )
    _, html_path = write_dataset_manifest_report(manifest, paths)
    return html_path, manifest.to_dict()


def account_backtest_symbol(
    symbol: str,
    instrument_kind: str | None = None,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    config: LedgerConfig | None = None,
    root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
    configured = load_instruments(paths.root)
    matched = next((item for item in configured if item.symbol == symbol), None)
    if matched is None:
        raise ValueError(f"config/instruments.json 中没有 {symbol}，拒绝读取未配置路径")
    if instrument_kind is None:
        instrument_kind = matched.kind
    elif instrument_kind != matched.kind:
        raise ValueError("instrument_kind 与 config/instruments.json 不一致")
    manifest = generate_dataset_manifest(paths)
    manifest_item = next((item for item in manifest.files if item.symbol == symbol), None)
    if manifest_item is None:
        raise ValueError(f"数据清单中没有 {symbol}")
    if manifest_item.error_codes:
        raise ValueError(f"{symbol} 数据健康检查失败：{manifest_item.error_codes}")
    prices = read_curated(symbol, paths)
    curated_path = (paths.root / manifest_item.relative_path).resolve()
    if file_sha256(curated_path) != manifest_item.sha256:
        raise RuntimeError(f"{symbol} 在清单生成后发生变化，请重试")
    symbol_upstream_errors = manifest.upstream_errors.get(symbol, {})
    file_health_status = (
        "error"
        if manifest_item.error_codes
        else "warning"
        if manifest_item.warning_codes or symbol_upstream_errors
        else "pass"
    )
    data_health = LedgerDataHealth(
        manifest_generated_at=manifest.generated_at,
        as_of_date=manifest.as_of_date,
        manifest_health_status=manifest.health_status,
        file_health_status=file_health_status,
        start_date=manifest_item.start_date,
        end_date=manifest_item.end_date,
        sources=manifest_item.sources,
        business_days_stale=manifest_item.business_days_stale,
        warning_codes=manifest_item.warning_codes,
        upstream_errors=symbol_upstream_errors,
        update_summary_end=manifest.update_summary_end,
    )
    result = run_account_ledger(
        prices,
        instrument_kind=instrument_kind,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        config=config,
        dataset_id=manifest.dataset_id,
        curated_file_sha256=manifest_item.sha256,
        data_health=data_health,
    )
    html_path, _, _, _, _ = write_account_ledger_report(result, paths)
    payload: dict[str, object] = {
        "settings": result.settings_dict(),
        "metrics": result.metrics.to_dict(),
        "benchmark_metrics": result.benchmark_metrics.to_dict(),
        "checks": result.checks.to_dict(),
        "benchmark_checks": result.benchmark_checks.to_dict(),
        "trades": len(result.trades),
        "final_equity": result.final_equity,
        "benchmark_final_equity": result.benchmark_final_equity,
    }
    return html_path, payload


def backtest_symbol(
    symbol: str,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    cost_bps: float = 5.0,
    root: Path | None = None,
) -> tuple[Path, dict[str, float | int]]:
    paths = get_paths(root)
    prices = read_curated(symbol, paths)
    result = run_backtest(
        prices,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
    )
    html_path, _, _ = write_backtest_report(result, paths)
    return html_path, result.metrics.to_dict()


def experiment_symbol(
    symbol: str,
    split_date: date,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    cost_bps: float = 5.0,
    root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
    prices = read_curated(symbol, paths)
    result = run_split_experiment(
        prices,
        split_date=split_date,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
    )
    html_path, _, _, _ = write_split_experiment_report(result, paths)
    payload = {
        "settings": result.settings_dict(),
        "development": result.development.to_dict(),
        "out_of_sample": result.out_of_sample.to_dict(),
        "trades": len(result.trades),
    }
    return html_path, payload


def walk_forward_symbol(
    symbol: str,
    first_oos_date: date,
    fold_months: int = 12,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    cost_bps: float = 5.0,
    root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
    prices = read_curated(symbol, paths)
    result = run_walk_forward(
        prices,
        first_oos_date=first_oos_date,
        fold_months=fold_months,
        strategy=strategy,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
    )
    html_path, _, _, _ = write_walk_forward_report(result, paths)
    payload = {
        "settings": result.settings_dict(),
        "fold_count": len(result.folds),
        "positive_folds": result.positive_folds,
        "beats_benchmark_folds": result.beats_benchmark_folds,
        "aggregate_metrics": result.aggregate_metrics.to_dict(),
        "aggregate_benchmark_metrics": result.aggregate_benchmark_metrics.to_dict(),
        "execution_checks": result.execution_checks.to_dict(),
    }
    return html_path, payload


def cost_stress_symbol(
    symbol: str,
    first_oos_date: date,
    cost_scenarios_bps: tuple[float, ...] = (5.0, 10.0, 20.0, 50.0),
    fold_months: int = 12,
    short_window: int = 20,
    long_window: int = 60,
    root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
    prices = read_curated(symbol, paths)
    result = run_cost_sensitivity(
        prices,
        first_oos_date=first_oos_date,
        cost_scenarios_bps=cost_scenarios_bps,
        fold_months=fold_months,
        short_window=short_window,
        long_window=long_window,
    )
    html_path, _, _, _ = write_cost_sensitivity_report(result, paths)
    payload = {
        "settings": result.settings_dict(),
        "monotonic_non_increasing": result.monotonic_non_increasing,
        "high_cost_return_change": result.high_cost_return_change,
        "scenarios": [scenario.to_dict() for scenario in result.scenarios],
    }
    return html_path, payload


def parameter_test_symbol(
    symbol: str,
    first_oos_date: date,
    short_windows: tuple[int, ...] = (10, 20, 30),
    long_windows: tuple[int, ...] = (40, 60, 90),
    reference_pair: tuple[int, int] = (20, 60),
    fold_months: int = 12,
    cost_bps: float = 5.0,
    root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
    prices = read_curated(symbol, paths)
    result = run_parameter_sensitivity(
        prices,
        first_oos_date=first_oos_date,
        short_windows=short_windows,
        long_windows=long_windows,
        reference_pair=reference_pair,
        fold_months=fold_months,
        cost_bps=cost_bps,
    )
    html_path, _, _, _ = write_parameter_sensitivity_report(result, paths)
    payload = {
        "settings": result.settings_dict(),
        "total_scenarios": result.total_scenarios,
        "profitable_scenarios": result.profitable_scenarios,
        "beats_benchmark_scenarios": result.beats_benchmark_scenarios,
        "median_total_return": result.median_total_return,
        "return_range": result.return_range,
        "scenarios": [scenario.to_dict() for scenario in result.scenarios],
    }
    return html_path, payload


def execution_test_symbol(
    symbol: str,
    first_oos_date: date,
    instrument_kind: str | None = None,
    short_window: int = 20,
    long_window: int = 60,
    cost_bps: float = 5.0,
    forced_delay_days: int = 1,
    lock_threshold_pct: float = 0.095,
    root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
    prices = read_curated(symbol, paths)
    if instrument_kind is None:
        config_path = paths.root / "config" / "instruments.json"
        configured = load_instruments(paths.root) if config_path.exists() else []
        matched = next((item for item in configured if item.symbol == symbol), None)
        instrument_kind = matched.kind if matched else "unknown"
    result = run_execution_feasibility(
        prices,
        first_oos_date=first_oos_date,
        instrument_kind=instrument_kind,
        short_window=short_window,
        long_window=long_window,
        cost_bps=cost_bps,
        forced_delay_days=forced_delay_days,
        lock_threshold_pct=lock_threshold_pct,
    )
    html_path, _, _, _ = write_execution_feasibility_report(result, paths)
    payload = {
        "settings": result.settings_dict(),
        "benchmark_metrics": result.benchmark_metrics.to_dict(),
        "scenarios": [
            {
                **scenario.to_dict(),
                "return_change_vs_ideal": result.return_change_vs_ideal(scenario),
                "position_difference_days": result.position_difference_days(scenario),
            }
            for scenario in result.scenarios
        ],
    }
    return html_path, payload


def validate_curated_data(root: Path | None = None) -> dict[str, Any]:
    paths = get_paths(root)
    files = sorted(paths.curated.glob("*.parquet"))
    file_reports: list[dict[str, object]] = []
    error_count = 0
    warning_count = 0
    for path in files:
        frame = pd.read_parquet(path)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        issues = validate_daily_prices(frame)
        file_reports.append(
            {
                "file": str(path.relative_to(paths.root)),
                "rows": int(len(frame)),
                "issues": [issue.to_dict() for issue in issues],
            }
        )
        error_count += sum(issue.severity == "error" for issue in issues)
        warning_count += sum(issue.severity == "warning" for issue in issues)
    return {
        "files": file_reports,
        "error_count": error_count,
        "warning_count": warning_count,
    }
