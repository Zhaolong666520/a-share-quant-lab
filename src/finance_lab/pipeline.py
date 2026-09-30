from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import pandas as pd

from finance_lab.atomic_io import published_parquet_files, write_text_atomic
from finance_lab.backtest import run_backtest
from finance_lab.config import Instrument, ProjectPaths, get_paths, load_instruments
from finance_lab.cost_sensitivity import run_cost_sensitivity
from finance_lab.cost_sensitivity_report import write_cost_sensitivity_report
from finance_lab.data_identity import assert_data_identity, validate_data_identity
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
from finance_lab.market_data_lock import market_data_lock
from finance_lab.market_data_state import (
    MarketUpdateBatch,
    assert_market_data_readable,
    market_update_batch,
)
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
from finance_lab.strategy_comparison import run_strategy_comparison
from finance_lab.strategy_comparison_report import write_strategy_comparison_report
from finance_lab.validation import (
    assert_valid_daily_prices,
    compare_sources,
    validate_daily_prices,
)
from finance_lab.walk_forward import run_walk_forward
from finance_lab.walk_forward_report import write_walk_forward_report


class ReportWriteError(RuntimeError):
    """A report could not be fully published; some files may remain."""


_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")


def _market_snapshot_bound(action: Callable[_Params, _Result]) -> Callable[_Params, _Result]:
    """Keep compliant data updates out of a report's read-to-publication window."""
    parameters = signature(action)

    @wraps(action)
    def wrapped(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        root = parameters.bind(*args, **kwargs).arguments.get("root")
        with market_data_lock(get_paths(root).data):
            return action(*args, **kwargs)

    return wrapped


@dataclass
class UpdateRecord:
    symbol: str
    name: str
    rows: int = 0
    primary_source: str | None = None
    raw_files: list[str] = field(default_factory=list)
    curated_file: str | None = None
    source_errors: dict[str, str] = field(default_factory=dict)
    source_diagnostics: dict[str, dict[str, object]] = field(default_factory=dict)
    source_comparison: dict[str, object] | None = None
    status: str = "failed"


_SOURCE_ERROR_STAGES = frozenset(
    {
        "unknown", "import", "login", "request", "query", "iteration",
        "response", "validation", "logout",
    }
)


def _checked_source_counts(frame: pd.DataFrame) -> dict[str, object]:
    counts = frame.attrs.get("source_diagnostics")
    if counts is None:
        return {
            "received_rows": len(frame),
            "accepted_rows": len(frame),
            "rejected_rows": 0,
            "rejection_reasons": {},
        }
    keys = {"received_rows", "accepted_rows", "rejected_rows", "rejection_reasons"}
    valid = isinstance(counts, dict) and set(counts) == keys
    if valid:
        received = counts["received_rows"]
        accepted = counts["accepted_rows"]
        rejected = counts["rejected_rows"]
        reasons = counts["rejection_reasons"]
        valid = (
            type(received) is int
            and type(accepted) is int
            and type(rejected) is int
            and received >= 0
            and accepted == len(frame)
            and rejected == received - accepted
            and isinstance(reasons, dict)
            and set(reasons).issubset({"nontrading"})
            and all(type(value) is int and value >= 0 for value in reasons.values())
            and sum(reasons.values()) == rejected
        )
    if not valid:
        raise DataSourceError(
            "数据源行数诊断不符合契约",
            stage="validation",
            received_rows=len(frame),
            accepted_rows=0,
            rejected_rows=len(frame),
        )
    return counts


def _checked_failure_counts(error: DataSourceError) -> tuple[int, int, int]:
    received = error.received_rows
    accepted = error.accepted_rows
    rejected = error.rejected_rows
    if (
        type(received) is int
        and type(accepted) is int
        and type(rejected) is int
        and received >= 0
        and 0 <= accepted <= received
        and rejected == received - accepted
    ):
        return received, accepted, rejected
    return 0, 0, 0


def _fetch_instrument(
    instrument: Instrument,
    start: date,
    end: date,
    paths: ProjectPaths,
    batch: MarketUpdateBatch | None = None,
) -> UpdateRecord:
    record = UpdateRecord(symbol=instrument.symbol, name=instrument.name)
    frames: dict[str, pd.DataFrame] = {}
    fetchers = {"akshare": fetch_akshare, "baostock": fetch_baostock}
    for source, fetcher in fetchers.items():
        if batch is not None:
            batch.stage("fetch")
        frame: pd.DataFrame | None = None
        try:
            frame = fetcher(instrument, start, end)
            assert_valid_daily_prices(frame)
            assert_data_identity(frame, expected_symbol=instrument.symbol)
            counts = _checked_source_counts(frame)
            dates = pd.to_datetime(frame["trade_date"])
            record.source_diagnostics[source] = {
                **counts,
                "status": "filtered" if counts["rejected_rows"] else "success",
                "stage": "complete",
                "first_trade_date": dates.min().date().isoformat(),
                "last_trade_date": dates.max().date().isoformat(),
            }
            if batch is not None:
                batch.stage("raw")
            raw_path = save_raw_snapshot(frame, paths)
            frames[source] = frame
            record.raw_files.append(str(raw_path.relative_to(paths.root)))
        except DataSourceError as exc:
            stage = (
                exc.stage
                if isinstance(exc.stage, str) and exc.stage in _SOURCE_ERROR_STAGES
                else "unknown"
            )
            received, accepted, rejected = _checked_failure_counts(exc)
            record.source_errors[source] = f"{source} 数据源在 {stage} 阶段失败"
            record.source_diagnostics[source] = {
                "status": "failed",
                "stage": stage,
                "received_rows": received,
                "accepted_rows": accepted,
                "rejected_rows": rejected,
                "rejection_reasons": {},
                "first_trade_date": None,
                "last_trade_date": None,
            }
        except ValueError:
            received = len(frame) if frame is not None else 0
            stage = "validation" if frame is not None else "request"
            record.source_errors[source] = (
                f"{source} 数据校验失败：标的或日线字段不符合契约"
                if frame is not None
                else f"{source} 请求或解析失败"
            )
            record.source_diagnostics[source] = {
                "status": "failed",
                "stage": stage,
                "received_rows": received,
                "accepted_rows": 0,
                "rejected_rows": received,
                "rejection_reasons": {},
                "first_trade_date": None,
                "last_trade_date": None,
            }

    if not frames:
        return record

    primary_source = "akshare" if "akshare" in frames else next(iter(frames))
    primary = frames[primary_source]
    if batch is not None:
        batch.stage("curated")
    curated_path = save_curated(primary, paths)
    record.primary_source = primary_source
    record.rows = int(len(primary))
    record.curated_file = str(curated_path.relative_to(paths.root))
    record.status = "partial_sources" if record.source_errors else "success"
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
    with market_data_lock(paths.data):
        assert_market_data_readable(paths)
        instruments = load_instruments(paths.root)
        with market_update_batch(
            paths, start, end, tuple(instrument.symbol for instrument in instruments),
        ) as batch:
            return _update_market_data(start, end, paths, instruments, batch)


def _update_market_data(
    start: date,
    end: date,
    paths: ProjectPaths,
    instruments: list[Instrument],
    batch: MarketUpdateBatch,
) -> dict[str, Any]:
    records = [
        _fetch_instrument(instrument, start, end, paths, batch)
        for instrument in instruments
    ]
    successful = [record for record in records if record.rows > 0]
    failed_sources = sum(len(record.source_errors) for record in records)
    outcome = (
        "failed" if not successful else "partial_sources" if failed_sources else "success"
    )
    batch.stage("database")
    database_rows = sync_duckdb(paths) if successful else 0
    payload: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "successful_instruments": len(successful),
        "failed_sources": failed_sources,
        "status": outcome,
        "database_rows": database_rows,
        "records": [asdict(record) for record in records],
    }
    output = paths.outputs / "update_summary.json"
    batch.stage("summary")
    write_text_atomic(
        output,
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def create_demo_report(root: Path | None = None) -> tuple[Path, dict[str, float | int]]:
    paths = get_paths(root)
    prices = make_synthetic_daily_prices()
    assert_valid_daily_prices(prices)
    result = run_backtest(prices, strategy="sma", short_window=20, long_window=60)
    try:
        html_path, _, _ = write_backtest_report(
            result,
            paths,
            title="A-Share Quant Lab 离线演示报告（合成数据）",
        )
    except OSError as exc:
        raise ReportWriteError("离线演示报告写入失败") from exc
    return html_path, result.metrics.to_dict()


@_market_snapshot_bound
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
    try:
        _, html_path = write_dataset_manifest_report(manifest, paths)
    except OSError as exc:
        raise ReportWriteError("数据健康报告写入失败") from exc
    return html_path, manifest.to_dict()


@_market_snapshot_bound
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
    try:
        html_path, _, _, _, _ = write_account_ledger_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("账户回测报告写入失败") from exc
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


@_market_snapshot_bound
def backtest_symbol(
    symbol: str,
    strategy: str = "sma",
    short_window: int = 20,
    long_window: int = 60,
    momentum_lookback: int = 120,
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
        momentum_lookback=momentum_lookback,
        cost_bps=cost_bps,
    )
    try:
        html_path, _, _ = write_backtest_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("回测报告写入失败") from exc
    return html_path, result.metrics.to_dict()


@_market_snapshot_bound
def strategy_compare_symbol(
    symbol: str,
    split_date: date,
    short_window: int = 20,
    long_window: int = 60,
    momentum_lookback: int = 120,
    cost_bps: float = 5.0,
    root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    paths = get_paths(root)
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
        "warning"
        if manifest_item.warning_codes or symbol_upstream_errors
        else "pass"
    )
    result = run_strategy_comparison(
        prices,
        split_date=split_date,
        short_window=short_window,
        long_window=long_window,
        momentum_lookback=momentum_lookback,
        cost_bps=cost_bps,
        dataset_id=manifest.dataset_id,
        curated_file_sha256=manifest_item.sha256,
        data_health_status=file_health_status,
        manifest_health_status=manifest.health_status,
        manifest_generated_at=manifest.generated_at,
        manifest_as_of_date=manifest.as_of_date,
        business_days_stale=manifest_item.business_days_stale,
        data_warning_codes=manifest_item.warning_codes,
        upstream_errors=symbol_upstream_errors,
        data_start_date=manifest_item.start_date,
        data_end_date=manifest_item.end_date,
        data_sources=manifest_item.sources,
        data_adjustments=manifest_item.adjustments,
        data_volume_units=manifest_item.volume_units,
    )
    try:
        html_path, _, _, _ = write_strategy_comparison_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("多策略对比报告写入失败") from exc
    payload: dict[str, object] = {
        "settings": result.settings_dict(),
        "benchmark_metrics": result.benchmark_metrics.to_dict(),
        "scenarios": [scenario.to_dict() for scenario in result.scenarios],
    }
    return html_path, payload


@_market_snapshot_bound
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
    try:
        html_path, _, _, _ = write_split_experiment_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("样本外实验报告写入失败") from exc
    payload = {
        "settings": result.settings_dict(),
        "development": result.development.to_dict(),
        "out_of_sample": result.out_of_sample.to_dict(),
        "trades": len(result.trades),
    }
    return html_path, payload


@_market_snapshot_bound
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
    try:
        html_path, _, _, _ = write_walk_forward_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("滚动检验报告写入失败") from exc
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


@_market_snapshot_bound
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
    try:
        html_path, _, _, _ = write_cost_sensitivity_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("成本压力报告写入失败") from exc
    payload = {
        "settings": result.settings_dict(),
        "monotonic_non_increasing": result.monotonic_non_increasing,
        "high_cost_return_change": result.high_cost_return_change,
        "scenarios": [scenario.to_dict() for scenario in result.scenarios],
    }
    return html_path, payload


@_market_snapshot_bound
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
    try:
        html_path, _, _, _ = write_parameter_sensitivity_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("参数稳健性报告写入失败") from exc
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


@_market_snapshot_bound
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
    try:
        html_path, _, _, _ = write_execution_feasibility_report(result, paths)
    except OSError as exc:
        raise ReportWriteError("执行可行性报告写入失败") from exc
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
    with market_data_lock(paths.data):
        assert_market_data_readable(paths)
        return _validate_curated_data(paths)


def _validate_curated_data(paths: ProjectPaths) -> dict[str, Any]:
    files = published_parquet_files(paths.curated)
    file_reports: list[dict[str, object]] = []
    global_issues: list[dict[str, str]] = []
    error_count = 0
    warning_count = 0
    read_failed = False
    if not files:
        global_issues.append(
            {
                "severity": "error",
                "code": "no_curated_files",
                "message": "没有可校验的已发布整理行情文件",
            }
        )
        error_count += 1
    for path in files:
        try:
            frame = pd.read_parquet(path)
        except Exception:
            read_failed = True
            error_count += 1
            file_reports.append(
                {
                    "file": str(path.relative_to(paths.root)),
                    "rows": None,
                    "issues": [
                        {
                            "severity": "error",
                            "code": "unreadable_parquet",
                            "message": "已发布整理行情文件无法读取；保留原文件检查",
                        }
                    ],
                }
            )
            continue
        issues = validate_daily_prices(frame) + validate_data_identity(frame, filename=path.name)
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
        "issues": global_issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "status": "failed" if error_count else "passed",
        "stage": "discover" if not files else "read" if read_failed else "validate",
        "data_committed": False,
        "next_action": (
            "先取得有效的整理行情快照，再重新运行校验；不要删除临时文件或覆盖原始数据。"
            if not files
            else "保留失败文件，核对来源与快照后修复或重建，再重新运行校验。"
            if error_count
            else None
        ),
    }
