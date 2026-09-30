from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path

import duckdb

from finance_lab.atomic_io import published_parquet_files
from finance_lab.config import get_paths, initialize_instruments, load_instruments, project_root
from finance_lab.data_identity import canonical_symbol_stem
from finance_lab.data_manifest import DatasetManifest, generate_dataset_manifest
from finance_lab.filename_audit import audit_curated_filenames
from finance_lab.ledger import LedgerConfig
from finance_lab.market_data_lock import MarketDataLockError, market_data_lock
from finance_lab.market_data_state import (
    MarketDataStateError,
    assert_market_data_readable,
    market_update_status,
)
from finance_lab.paper_automation import (
    PaperAutomationStatusWriteError,
    run_paper_daily_automation,
)
from finance_lab.paper_pipeline import (
    PaperPortfolioNotFound,
    PaperReportWriteError,
    paper_init_portfolio,
    paper_run_portfolio,
    paper_status_portfolio,
)
from finance_lab.pipeline import (
    ReportWriteError,
    account_backtest_symbol,
    backtest_symbol,
    cost_stress_symbol,
    create_demo_report,
    data_health,
    execution_test_symbol,
    experiment_symbol,
    parameter_test_symbol,
    strategy_compare_symbol,
    update_market_data,
    validate_curated_data,
    walk_forward_symbol,
)
from finance_lab.recovery import prepare_recovery, verify_recovery
from finance_lab.recovery_activation import activate_candidate, verify_activation
from finance_lab.recovery_rebuild import rebuild_candidate, verify_candidate
from finance_lab.report_bundle import verify_report_bundle
from finance_lab.research_session import create_session, verify_session


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD")
    return parsed


def _parse_dataset_id(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise argparse.ArgumentTypeError("数据集 ID 必须是完整的 64 位十六进制 SHA-256")
    return value.lower()


def _parse_costs(value: str) -> tuple[float, ...]:
    parts = [item.strip() for item in value.split(",")]
    if any(not item for item in parts):
        raise argparse.ArgumentTypeError("成本列表不能包含空项")
    try:
        costs = tuple(float(item) for item in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("成本必须是逗号分隔的数字，例如 5,10,20,50") from exc
    if not costs:
        raise argparse.ArgumentTypeError("至少需要一个成本数字")
    if not all(math.isfinite(cost) and cost >= 0 for cost in costs):
        raise argparse.ArgumentTypeError("成本必须是非负有限数字")
    return costs


def _parse_windows(value: str) -> tuple[int, ...]:
    parts = [item.strip() for item in value.split(",")]
    if any(not item for item in parts):
        raise argparse.ArgumentTypeError("窗口列表不能包含空项")
    try:
        windows = tuple(int(item) for item in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("窗口必须是逗号分隔的整数，例如 10,20,30") from exc
    if not windows:
        raise argparse.ArgumentTypeError("至少需要一个窗口数字")
    if any(window <= 0 for window in windows) or len(windows) != len(set(windows)):
        raise argparse.ArgumentTypeError("窗口必须是互不重复的正整数")
    return windows


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _missing_input(error_code: str, stage: str, message: str, next_action: str) -> int:
    _print_json(
        {
            "ok": False,
            "error_code": error_code,
            "stage": stage,
            "data_committed": False,
            "message": message,
            "next_action": next_action,
        }
    )
    return 3


def _report_write_failure() -> int:
    _print_json(
        {
            "ok": False,
            "status": "failed",
            "error_code": "report_write_failed",
            "stage": "write_report",
            "data_committed": False,
            "report_complete": False,
            "current_report_state": "unknown",
            "message": "报告未完整发布；输出目录可能保留本次或上次运行的部分文件。",
            "next_action": (
                "先检查输出目录和磁盘权限；修复后重试，并用 report-verify 核验报告，"
                "不要把现有文件当作本次成功结果。"
            ),
        }
    )
    return 1


_BATCH_SNAPSHOT_COMMANDS = frozenset(
    {
        "account-all",
        "experiment-all",
        "walk-forward-all",
        "cost-stress-all",
        "parameter-test-all",
        "execution-test-all",
        "compare-all",
        "all",
    }
)


def _dataset_preflight(
    expected_id: str, *, check_only: bool = False
) -> tuple[DatasetManifest | None, int | None]:
    incomplete = "本次数据集核验未完成" if check_only else "本次研究报告未开始"
    try:
        manifest = generate_dataset_manifest(get_paths())
    except MarketDataLockError:
        raise
    except Exception:
        _print_json(
            {
                "ok": False,
                "error_code": "dataset_preflight_failed",
                "stage": "verify_dataset",
                "data_committed": False,
                "report_batch_complete": False,
                "reports_confirmed": [],
                "message": f"无法核验已发布行情的数据集身份；{incomplete}。",
                "next_action": "先运行 manifest 检查本地行情和数据健康状态，修复后重试。",
            }
        )
        return None, 3
    if manifest.health_status == "error":
        _print_json(
            {
                "ok": False,
                "error_code": "dataset_health_failed",
                "stage": "verify_dataset",
                "data_committed": False,
                "report_batch_complete": False,
                "reports_confirmed": [],
                "message": f"数据集健康检查存在错误；{incomplete}。",
                "next_action": "先运行 manifest 查看错误并修复行情，再重试。",
            }
        )
        return None, 3
    if manifest.dataset_id != expected_id:
        _print_json(
            {
                "ok": False,
                "error_code": "dataset_id_mismatch",
                "stage": "verify_dataset",
                "data_committed": False,
                "report_batch_complete": False,
                "reports_confirmed": [],
                "expected_dataset_id": expected_id,
                "actual_dataset_id": manifest.dataset_id,
                "message": f"已发布行情与预期数据集 ID 不一致；{incomplete}。",
                "next_action": (
                    "核对更新记录；若要研究新版行情，先运行 manifest 并记录新的完整 ID。"
                ),
            }
        )
        return None, 3
    return manifest, None


def _batch_snapshot_busy() -> int:
    _print_json(
        {
            "ok": False,
            "status": "failed",
            "error_code": "market_snapshot_busy",
            "stage": "acquire_market_snapshot",
            "data_committed": False,
            "report_batch_complete": False,
            "reports_confirmed": [],
            "message": "已有行情任务占用项目数据；本次批量运行未开始。",
            "next_action": "等待现有任务结束并核对行情状态后，再重试整个批次。",
        }
    )
    return 1


def _research_snapshot_busy() -> int:
    _print_json(
        {
            "ok": False,
            "error_code": "market_snapshot_busy",
            "stage": "acquire_market_snapshot",
            "data_committed": False,
            "report_complete": False,
            "reports_confirmed": [],
            "message": "已有行情任务占用项目数据；本次研究报告未开始。",
            "next_action": "等待现有任务结束并核对行情状态后，使用相同数据集 ID 重试。",
        }
    )
    return 1


def _dataset_check_busy() -> int:
    _print_json(
        {
            "ok": False,
            "error_code": "market_snapshot_busy",
            "stage": "acquire_market_snapshot",
            "data_committed": False,
            "message": "已有行情任务占用项目数据；本次数据集核验未开始。",
            "next_action": "等待现有任务结束后，使用相同预期 ID 重试核验。",
        }
    )
    return 1


def _filename_audit_failure(*, busy: bool) -> int:
    _print_json(
        {
            "ok": False,
            "status": "failed",
            "error_code": "market_snapshot_busy" if busy else "filename_audit_failed",
            "stage": "acquire_market_snapshot" if busy else "inspect_curated_filenames",
            "data_committed": False,
            "message": (
                "已有行情任务占用项目数据；本次文件名审计未开始。"
                if busy
                else "无法完成整理行情文件名审计；没有修改行情文件。"
            ),
            "next_action": "检查行情任务或目录权限后重试；不要自动删除或重命名已有文件。",
        }
    )
    return 1


def _market_update_interrupted(*, includes_reports: bool = False) -> int:
    try:
        diagnostic = market_update_status(get_paths())
    except Exception:
        diagnostic = None
    state = diagnostic.get("state") if diagnostic is not None else None
    phase = state.get("phase") if isinstance(state, dict) else None
    payload: dict[str, object] = {
        "ok": False,
        "status": "failed",
        "error_code": "market_update_interrupted",
        "stage": "update_market_data",
        "data_committed": None,
        "market_update_status": diagnostic.get("status") if diagnostic else "unknown",
        "market_data_blocked": diagnostic.get("blocked") if diagnostic else None,
        "market_update_phase": phase,
        "summary_state": "unknown",
        "message": "行情更新中断；可能已有部分文件发布，不能将旧摘要视为本次成功结果。",
        "next_action": (
            "保留行情、原始快照和状态文件；先运行 data-update-status 核对批次，"
            "确认已写入内容及恢复路径后再决定是否重试。"
        ),
    }
    if includes_reports:
        payload["report_batch_complete"] = False
        payload["reports_confirmed"] = []
    _print_json(payload)
    return 1


def _paper_report_write_failure(error: PaperReportWriteError) -> int:
    _print_json(
        {
            "ok": False,
            "status": "failed",
            "error_code": "paper_report_write_failed",
            "stage": "write_paper_report",
            "data_committed": error.ledger_committed,
            "ledger_committed": error.ledger_committed,
            "operation_status": error.operation_status,
            "portfolio_id": error.portfolio_id,
            "processed_dates": [item.isoformat() for item in error.processed_dates],
            "report_complete": False,
            "current_report_state": "unknown",
            "message": "模拟盘报告未完整发布；已有账本记录不会因报告失败而撤销。",
            "next_action": (
                "先修复输出目录或磁盘问题，再运行 paper-status 审计账本并重新生成报告；"
                "不要删除账本，也不要把已处理交易日当作未入账而重新初始化。"
            ),
        }
    )
    return 1


def _print_batch_results(label: str, reports: list[dict[str, object]]) -> None:
    for item in reports:
        report = item.get("report")
        if report is not None:
            print(f"{label}：{report}")
    _print_json(reports)


def _add_account_arguments(parser: argparse.ArgumentParser, include_symbol: bool) -> None:
    if include_symbol:
        parser.add_argument("--symbol", default="sh.510300")
    parser.add_argument("--strategy", choices=["sma", "buy_hold"], default="sma")
    parser.add_argument("--short", type=int, default=20)
    parser.add_argument("--long", type=int, default=60)
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument("--commission-bps", type=float, default=3.0)
    parser.add_argument("--minimum-commission", type=float, default=5.0)
    parser.add_argument("--sell-tax-bps", type=float, default=0.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)


def _ledger_config(args: argparse.Namespace) -> LedgerConfig:
    return LedgerConfig(
        initial_cash=args.initial_cash,
        lot_size=args.lot_size,
        commission_bps=args.commission_bps,
        minimum_commission=args.minimum_commission,
        sell_tax_bps=args.sell_tax_bps,
        slippage_bps=args.slippage_bps,
    )


def _paper_operation_payload(result: object) -> dict[str, object]:
    from finance_lab.paper_models import PaperOperationResult

    if not isinstance(result, PaperOperationResult):
        raise TypeError("模拟盘命令返回了无效结果")
    return {
        "status": result.status,
        "portfolio_id": result.portfolio_id,
        "processed_dates": [item.isoformat() for item in result.processed_dates],
        "accounts": [
            {
                "account_id": state.account_id,
                "last_trade_date": state.last_trade_date.isoformat(),
                "cash": state.cash,
                "shares": state.shares,
                "equity": state.equity,
                "drawdown": state.drawdown,
                "pending_order": (
                    {
                        "order_id": state.pending_order.order_id,
                        "signal_date": state.pending_order.signal_date.isoformat(),
                        "action": state.pending_order.action,
                        "attempt_count": state.pending_order.attempt_count,
                    }
                    if state.pending_order
                    else None
                ),
            }
            for state in result.states
        ],
        "report_path": str(result.report_path) if result.report_path else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finance-lab",
        description="A股日线数据与基础回测学习工具（不连接券商）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("demo", help="使用合成数据生成离线演示报告")
    subparsers.add_parser("init", help="在当前工作目录创建示例标的配置（不覆盖已有文件）")

    manifest = subparsers.add_parser("manifest", help="生成本地数据集指纹与健康报告")
    manifest.add_argument("--as-of", type=_parse_date, default=date.today())
    manifest.add_argument("--stale-after-business-days", type=int, default=3)
    dataset_check = subparsers.add_parser(
        "dataset-check", help="只读核验本地数据集是否仍匹配预期 ID"
    )
    dataset_check.add_argument(
        "--expected-dataset-id",
        required=True,
        type=_parse_dataset_id,
        help="manifest 输出的完整 64 位数据集 ID",
    )
    subparsers.add_parser("filename-audit", help="只读审计整理行情文件名的跨平台别名风险")

    paper_init = subparsers.add_parser("paper-init", help="初始化固定双策略前向模拟盘")
    paper_init.add_argument("--portfolio", default="default")
    paper_init.add_argument("--initial-cash", type=float, default=100_000.0)
    paper_init.add_argument("--lot-size", type=int, default=100)
    paper_init.add_argument("--commission-bps", type=float, default=3.0)
    paper_init.add_argument("--minimum-commission", type=float, default=5.0)
    paper_init.add_argument("--sell-tax-bps", type=float, default=0.0)
    paper_init.add_argument("--slippage-bps", type=float, default=2.0)
    paper_init.add_argument("--as-of", type=_parse_date, default=date.today())
    paper_init.add_argument("--stale-after-business-days", type=int, default=3)

    paper_run = subparsers.add_parser("paper-run", help="处理模拟盘全部未处理本地日线")
    paper_run.add_argument("--portfolio", default="default")
    paper_run.add_argument("--as-of", type=_parse_date, default=date.today())
    paper_run.add_argument("--stale-after-business-days", type=int, default=3)

    paper_status = subparsers.add_parser("paper-status", help="只读审计模拟盘当前状态")
    paper_status.add_argument("--portfolio", default="default")

    paper_daily = subparsers.add_parser(
        "paper-daily",
        help="执行一次带成功/失败留证的每日模拟盘流程",
    )
    paper_daily.add_argument("--portfolio", default="default")
    paper_daily.add_argument("--start", type=_parse_date, default=date(2018, 1, 1))
    paper_daily.add_argument("--as-of", type=_parse_date, default=date.today())
    paper_daily.add_argument("--stale-after-business-days", type=int, default=3)

    account = subparsers.add_parser("account", help="对可交易标的运行现金和整数份额账本")
    _add_account_arguments(account, include_symbol=True)

    account_all = subparsers.add_parser(
        "account-all",
        help="对全部可交易配置标的运行现金和整数份额账本",
    )
    _add_account_arguments(account_all, include_symbol=False)

    fetch = subparsers.add_parser("fetch", help="联网更新真实日线数据")
    fetch.add_argument("--start", type=_parse_date, default=date(2018, 1, 1))
    fetch.add_argument("--end", type=_parse_date, default=date.today())

    backtest = subparsers.add_parser("backtest", help="对本地整理数据运行回测")
    backtest.add_argument("--symbol", default="sh.000300")
    backtest.add_argument("--strategy", choices=["sma", "buy_hold", "momentum"], default="sma")
    backtest.add_argument("--short", type=int, default=20)
    backtest.add_argument("--long", type=int, default=60)
    backtest.add_argument("--momentum-lookback", type=int, default=120)
    backtest.add_argument("--cost-bps", type=float, default=5.0)

    experiment = subparsers.add_parser("experiment", help="运行固定切分的样本外实验")
    experiment.add_argument("--symbol", default="sh.000300")
    experiment.add_argument("--split-date", type=_parse_date, default=date(2023, 1, 1))
    experiment.add_argument("--strategy", choices=["sma", "buy_hold"], default="sma")
    experiment.add_argument("--short", type=int, default=20)
    experiment.add_argument("--long", type=int, default=60)
    experiment.add_argument("--cost-bps", type=float, default=5.0)

    experiment_all = subparsers.add_parser("experiment-all", help="对全部配置标的运行样本外实验")
    experiment_all.add_argument("--split-date", type=_parse_date, default=date(2023, 1, 1))
    experiment_all.add_argument("--short", type=int, default=20)
    experiment_all.add_argument("--long", type=int, default=60)
    experiment_all.add_argument("--cost-bps", type=float, default=5.0)

    walk = subparsers.add_parser("walk-forward", help="运行固定参数的滚动样本外检验")
    walk.add_argument("--symbol", default="sh.000300")
    walk.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    walk.add_argument("--fold-months", type=int, default=12)
    walk.add_argument("--strategy", choices=["sma", "buy_hold"], default="sma")
    walk.add_argument("--short", type=int, default=20)
    walk.add_argument("--long", type=int, default=60)
    walk.add_argument("--cost-bps", type=float, default=5.0)

    walk_all = subparsers.add_parser("walk-forward-all", help="对全部配置标的运行滚动检验")
    walk_all.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    walk_all.add_argument("--fold-months", type=int, default=12)
    walk_all.add_argument("--short", type=int, default=20)
    walk_all.add_argument("--long", type=int, default=60)
    walk_all.add_argument("--cost-bps", type=float, default=5.0)

    cost = subparsers.add_parser("cost-stress", help="运行固定策略的交易成本压力测试")
    cost.add_argument("--symbol", default="sh.000300")
    cost.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    cost.add_argument("--fold-months", type=int, default=12)
    cost.add_argument("--short", type=int, default=20)
    cost.add_argument("--long", type=int, default=60)
    cost.add_argument("--costs", type=_parse_costs, default=(5.0, 10.0, 20.0, 50.0))

    cost_all = subparsers.add_parser("cost-stress-all", help="对全部标的运行成本压力测试")
    cost_all.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    cost_all.add_argument("--fold-months", type=int, default=12)
    cost_all.add_argument("--short", type=int, default=20)
    cost_all.add_argument("--long", type=int, default=60)
    cost_all.add_argument("--costs", type=_parse_costs, default=(5.0, 10.0, 20.0, 50.0))

    parameters = subparsers.add_parser("parameter-test", help="运行均线参数稳健性检验")
    parameters.add_argument("--symbol", default="sh.000300")
    parameters.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    parameters.add_argument("--fold-months", type=int, default=12)
    parameters.add_argument("--shorts", type=_parse_windows, default=(10, 20, 30))
    parameters.add_argument("--longs", type=_parse_windows, default=(40, 60, 90))
    parameters.add_argument("--reference-short", type=int, default=20)
    parameters.add_argument("--reference-long", type=int, default=60)
    parameters.add_argument("--cost-bps", type=float, default=5.0)

    parameters_all = subparsers.add_parser(
        "parameter-test-all",
        help="对全部标的运行均线参数稳健性检验",
    )
    parameters_all.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    parameters_all.add_argument("--fold-months", type=int, default=12)
    parameters_all.add_argument("--shorts", type=_parse_windows, default=(10, 20, 30))
    parameters_all.add_argument("--longs", type=_parse_windows, default=(40, 60, 90))
    parameters_all.add_argument("--reference-short", type=int, default=20)
    parameters_all.add_argument("--reference-long", type=int, default=60)
    parameters_all.add_argument("--cost-bps", type=float, default=5.0)

    execution = subparsers.add_parser(
        "execution-test",
        help="运行延迟成交与日线阻塞代理的执行可行性压力测试",
    )
    execution.add_argument("--symbol", default="sh.000300")
    execution.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    execution.add_argument("--short", type=int, default=20)
    execution.add_argument("--long", type=int, default=60)
    execution.add_argument("--cost-bps", type=float, default=5.0)
    execution.add_argument("--delay-days", type=int, default=1)
    execution.add_argument("--lock-threshold", type=float, default=0.095)

    execution_all = subparsers.add_parser(
        "execution-test-all",
        help="对全部配置标的运行执行可行性压力测试",
    )
    execution_all.add_argument("--first-oos-date", type=_parse_date, default=date(2021, 1, 1))
    execution_all.add_argument("--short", type=int, default=20)
    execution_all.add_argument("--long", type=int, default=60)
    execution_all.add_argument("--cost-bps", type=float, default=5.0)
    execution_all.add_argument("--delay-days", type=int, default=1)
    execution_all.add_argument("--lock-threshold", type=float, default=0.095)

    compare = subparsers.add_parser(
        "compare",
        help="在相同切分和成本下对比买入持有、双均线与时间序列动量",
    )
    compare.add_argument("--symbol", default="sh.000300")
    compare.add_argument("--split-date", type=_parse_date, default=date(2023, 1, 1))
    compare.add_argument("--short", type=int, default=20)
    compare.add_argument("--long", type=int, default=60)
    compare.add_argument("--momentum-lookback", type=int, default=120)
    compare.add_argument("--cost-bps", type=float, default=5.0)

    compare_all = subparsers.add_parser(
        "compare-all",
        help="对全部配置标的生成固定多策略对比报告",
    )
    compare_all.add_argument("--split-date", type=_parse_date, default=date(2023, 1, 1))
    compare_all.add_argument("--short", type=int, default=20)
    compare_all.add_argument("--long", type=int, default=60)
    compare_all.add_argument("--momentum-lookback", type=int, default=120)
    compare_all.add_argument("--cost-bps", type=float, default=5.0)

    subparsers.add_parser("validate", help="检查本地整理数据")
    report_verify = subparsers.add_parser("report-verify", help="只读核验历史报告完成标记")
    report_verify.add_argument("--report", required=True, type=Path, help="报告 HTML 文件路径")
    subparsers.add_parser("data-update-status", help="只读诊断行情更新批次与隔离状态")
    recovery_prepare = subparsers.add_parser(
        "data-recovery-prepare", help="创建可校验恢复材料，不修复或切换正式数据"
    )
    recovery_prepare.add_argument("--destination", required=True, type=Path)
    recovery_verify = subparsers.add_parser(
        "data-recovery-verify", help="重新校验恢复材料，不解除行情隔离"
    )
    recovery_verify.add_argument("--bundle", required=True, type=Path)
    rebuild = subparsers.add_parser(
        "data-recovery-rebuild", help="按明确指定的原始快照生成独立行情候选"
    )
    rebuild.add_argument("--bundle", required=True, type=Path)
    rebuild.add_argument("--selection", required=True, type=Path)
    rebuild.add_argument("--destination", required=True, type=Path)
    candidate_verify = subparsers.add_parser(
        "data-recovery-candidate-verify", help="重新核验独立行情候选和模拟账本"
    )
    candidate_verify.add_argument("--bundle", required=True, type=Path)
    candidate_verify.add_argument("--candidate", required=True, type=Path)

    all_command = subparsers.add_parser("all", help="更新数据并回测全部配置标的")
    all_command.add_argument("--start", type=_parse_date, default=date(2018, 1, 1))
    all_command.add_argument("--end", type=_parse_date, default=date.today())
    for research_parser in (
        account,
        account_all,
        backtest,
        experiment,
        experiment_all,
        walk,
        walk_all,
        cost,
        cost_all,
        parameters,
        parameters_all,
        execution,
        execution_all,
        compare,
        compare_all,
    ):
        research_parser.add_argument(
            "--expected-dataset-id",
            type=_parse_dataset_id,
            help="仅在已发布行情与 manifest 的完整数据集 ID 一致时生成报告",
        )
        research_parser.add_argument("--session", type=Path, help="读取已冻结研究会话")
    session_create = subparsers.add_parser("research-session-create", help="冻结完整研究数据与配置")
    session_create.add_argument("--destination", required=True, type=Path)
    session_create.add_argument("--expected-dataset-id", type=_parse_dataset_id)
    session_verify = subparsers.add_parser("research-session-verify", help="校验冻结研究会话")
    session_verify.add_argument("--session", required=True, type=Path)
    activate = subparsers.add_parser("data-recovery-activate", help="人工确认后验收至全新项目目录")
    activate.add_argument("--bundle", required=True, type=Path)
    activate.add_argument("--candidate", required=True, type=Path)
    activate.add_argument("--destination", required=True, type=Path)
    activate.add_argument("--confirm-candidate-id", required=True)
    accepted = subparsers.add_parser("data-recovery-acceptance-verify", help="复核恢复验收凭据")
    accepted.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {
        "research-session-create",
        "research-session-verify",
        "data-recovery-activate",
        "data-recovery-acceptance-verify",
    }:
        try:
            if args.command == "research-session-create":
                result = create_session(
                    project_root(), args.destination, expected_dataset_id=args.expected_dataset_id
                )
            elif args.command == "research-session-verify":
                result = verify_session(args.session)
            elif args.command == "data-recovery-activate":
                result = activate_candidate(
                    args.bundle,
                    args.candidate,
                    args.destination,
                    confirm_candidate_id=args.confirm_candidate_id,
                )
            else:
                result = verify_activation(args.root)
            _print_json({"ok": True, **result})
            return 0
        except Exception as exc:
            _print_json(
                {
                    "ok": False,
                    "error_code": "workspace_acceptance_failed",
                    "error_type": type(exc).__name__,
                    "message": "验收失败；保留来源和未完成目录，不要切换或继续研究。",
                }
            )
            return 1
    session = getattr(args, "session", None)
    if session is None and os.path.lexists(project_root() / "data/research_session.json"):
        if args.command not in {"init", "fetch", "all", "paper-init", "paper-run", "paper-daily"}:
            session = project_root().parent
    if session is not None:
        previous = os.environ.get("FINANCE_LAB_ROOT")
        try:
            receipt = verify_session(session)
            expected = getattr(args, "expected_dataset_id", None)
            if expected is not None and expected != receipt["dataset_id"]:
                raise ValueError("session dataset mismatch")
            os.environ["FINANCE_LAB_ROOT"] = str(session.absolute() / "snapshot")
            args.expected_dataset_id = receipt["dataset_id"]
            with market_data_lock(get_paths().data):
                if verify_session(session) != receipt:
                    raise ValueError("session receipt changed before command")
                result_code = _dispatch(args)
                if verify_session(session) != receipt:
                    raise ValueError("session receipt changed during command")
                return result_code
        except Exception as exc:
            _print_json(
                {
                    "ok": False,
                    "error_code": "research_session_invalid",
                    "error_type": type(exc).__name__,
                    "message": "研究会话校验失败，不可将输出视为可信会话结果。",
                }
            )
            return 1
        finally:
            if previous is None:
                os.environ.pop("FINANCE_LAB_ROOT", None)
            else:
                os.environ["FINANCE_LAB_ROOT"] = previous
    return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
    if os.path.lexists(project_root() / "data/research_session.json") and args.command in {
        "init",
        "fetch",
        "all",
        "paper-init",
        "paper-run",
        "paper-daily",
    }:
        _print_json({"ok": False, "error_code": "frozen_session_read_only"})
        return 1
    if args.command == "filename-audit":
        try:
            paths = get_paths()
            with market_data_lock(paths.data):
                audit = audit_curated_filenames(paths.curated)
        except MarketDataLockError:
            return _filename_audit_failure(busy=True)
        except Exception:
            return _filename_audit_failure(busy=False)
        _print_json(audit)
        return 0 if audit["ok"] else 3
    expected_id = getattr(args, "expected_dataset_id", None)
    if args.command in _BATCH_SNAPSHOT_COMMANDS or expected_id is not None:
        try:
            with market_data_lock(get_paths().data):
                if expected_id is not None:
                    checked_manifest, preflight_result = _dataset_preflight(
                        expected_id, check_only=args.command == "dataset-check"
                    )
                    if preflight_result is not None:
                        return preflight_result
                    if args.command == "dataset-check":
                        assert checked_manifest is not None
                        _print_json(
                            {
                                "ok": True,
                                "status": "verified",
                                "expected_dataset_id": expected_id,
                                "dataset_id": checked_manifest.dataset_id,
                                "health_status": checked_manifest.health_status,
                                "as_of_date": checked_manifest.as_of_date.isoformat(),
                                "total_rows": checked_manifest.total_rows,
                                "file_count": len(checked_manifest.files),
                                "data_committed": False,
                            }
                        )
                        return 0
                return _run_command(args)
        except MarketDataLockError:
            if args.command in _BATCH_SNAPSHOT_COMMANDS:
                return _batch_snapshot_busy()
            if args.command == "dataset-check":
                return _dataset_check_busy()
            return _research_snapshot_busy()
    return _run_command(args)


def _run_command(args: argparse.Namespace) -> int:
    batch_progress: list[dict[str, object]] = []
    batch_current_symbol: str | None = None
    try:
        if args.command == "init":
            path, created = initialize_instruments()
            print(f"{'已创建示例配置' if created else '配置已存在，未覆盖'}：{path}")
            return 0
        if args.command in {
            "account",
            "account-all",
            "experiment-all",
            "walk-forward-all",
            "cost-stress-all",
            "parameter-test-all",
            "execution-test-all",
            "compare-all",
        }:
            config_root = project_root()
            if not (config_root / "config" / "instruments.json").is_file():
                return _missing_input(
                    "missing_instruments_config",
                    "load_config",
                    (
                        "缺少 config/instruments.json，未生成账户账本报告。"
                        if args.command == "account"
                        else "缺少 config/instruments.json，未生成批量研究报告。"
                    ),
                    "先准备 config/instruments.json 并确认标的配置有效，再重试。",
                )
            try:
                load_instruments(config_root)
            except (json.JSONDecodeError, UnicodeError, TypeError, ValueError, KeyError):
                return _missing_input(
                    "invalid_instruments_config",
                    "load_config",
                    "config/instruments.json 格式或结构无效，未生成研究报告。",
                    "检查 JSON 格式，确保列表非空且每个标的包含 code、exchange、name 和 kind。",
                )
        if args.command in {
            "account",
            "backtest",
            "experiment",
            "walk-forward",
            "cost-stress",
            "parameter-test",
            "execution-test",
            "compare",
        }:
            paths = get_paths()
            filename = f"{canonical_symbol_stem(args.symbol)}.parquet"
            with market_data_lock(paths.data):
                assert_market_data_readable(paths)
                if not (paths.curated / filename).exists():
                    return _missing_input(
                        "missing_symbol_data",
                        "load_curated",
                        "缺少请求标的的已发布整理行情，未生成研究报告。",
                        "先更新并校验该标的行情，再用相同标的重试。",
                    )
        if args.command in {"data-recovery-rebuild", "data-recovery-candidate-verify"}:
            try:
                candidate_report = (
                    rebuild_candidate(args.bundle, args.selection, args.destination)
                    if args.command == "data-recovery-rebuild"
                    else verify_candidate(args.bundle, args.candidate)
                )
            except Exception as exc:
                _print_json(
                    {
                        "ok": False,
                        "ready_for_use": False,
                        "error_type": type(exc).__name__,
                        "candidate_path": str(
                            (
                                args.destination
                                if args.command == "data-recovery-rebuild"
                                else args.candidate
                            )
                            .expanduser()
                            .absolute()
                        ),
                        "message": "候选重建或核验失败；保留目标目录，检查输入后使用全新目录重试。",
                    }
                )
                return 1
            _print_json(candidate_report)
            return 0
        if args.command in {"data-recovery-prepare", "data-recovery-verify"}:
            try:
                payload = (
                    prepare_recovery(
                        Path(
                            os.environ.get(
                                "FINANCE_LAB_ROOT", str(Path(__file__).absolute().parents[2])
                            )
                        ),
                        args.destination,
                    )
                    if args.command == "data-recovery-prepare"
                    else verify_recovery(args.bundle)
                )
            except Exception as exc:
                bundle = (
                    args.destination if args.command == "data-recovery-prepare" else args.bundle
                )
                _print_json(
                    {
                        "ok": False,
                        "ready_for_use": False,
                        "error_type": type(exc).__name__,
                        "stage": getattr(exc, "stage", "lock_or_validation"),
                        "bundle_path": str(bundle.expanduser().absolute()),
                        "message": "恢复材料操作失败；保留目录，检查路径、锁和 WAL 后重试。",
                    }
                )
                return 1
            _print_json(payload)
            return 0 if payload["ok"] else 1
        if args.command == "data-update-status":
            status_payload = market_update_status(get_paths())
            _print_json(status_payload)
            if status_payload["blocked"]:
                return 1
            if status_payload["source_outcome"] in {"failed", "partial_sources"}:
                return 2
            return 0
        if args.command == "demo":
            try:
                report, metrics = create_demo_report()
            except ReportWriteError:
                return _report_write_failure()
            print(f"离线演示已生成：{report}")
            _print_json(metrics)
            return 0
        if args.command == "manifest":
            if not published_parquet_files(get_paths().curated):
                return _missing_input(
                    "missing_curated_data",
                    "load_curated",
                    "缺少已发布的整理行情 Parquet 文件，未生成数据集健康报告。",
                    "先运行数据更新并校验，确认 data/curated 中有已发布的 Parquet 文件。",
                )
            try:
                report, manifest_payload = data_health(
                    as_of_date=args.as_of,
                    stale_after_business_days=args.stale_after_business_days,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"数据集健康报告已生成：{report}")
            _print_json(manifest_payload)
            return 0 if manifest_payload["health_status"] != "error" else 3
        if args.command == "paper-init":
            try:
                result = paper_init_portfolio(
                    portfolio_id=args.portfolio,
                    ledger_config=_ledger_config(args),
                    as_of_date=args.as_of,
                    stale_after_business_days=args.stale_after_business_days,
                )
            except PaperReportWriteError as exc:
                return _paper_report_write_failure(exc)
            if result.report_path:
                print(f"模拟盘报告已生成：{result.report_path}")
            _print_json(_paper_operation_payload(result))
            return 0
        if args.command == "paper-run":
            try:
                result = paper_run_portfolio(
                    portfolio_id=args.portfolio,
                    as_of_date=args.as_of,
                    stale_after_business_days=args.stale_after_business_days,
                )
            except PaperReportWriteError as exc:
                return _paper_report_write_failure(exc)
            if result.report_path:
                print(f"模拟盘报告已生成：{result.report_path}")
            _print_json(_paper_operation_payload(result))
            return 0
        if args.command == "paper-status":
            try:
                result = paper_status_portfolio(portfolio_id=args.portfolio)
            except PaperReportWriteError as exc:
                return _paper_report_write_failure(exc)
            if result.report_path:
                print(f"模拟盘报告已生成：{result.report_path}")
            _print_json(_paper_operation_payload(result))
            return 0
        if args.command == "paper-daily":
            try:
                automation = run_paper_daily_automation(
                    portfolio_id=args.portfolio,
                    start_date=args.start,
                    as_of_date=args.as_of,
                    stale_after_business_days=args.stale_after_business_days,
                )
            except PaperAutomationStatusWriteError as exc:
                _print_json(
                    {
                        "ok": False,
                        "status": "failed",
                        "error_code": "paper_automation_status_write_failed",
                        "stage": exc.stage,
                        "portfolio_id": exc.portfolio_id,
                        "market_data_committed": exc.market_data_committed,
                        "ledger_committed": exc.ledger_committed,
                        "processed_dates": list(exc.processed_dates),
                        "status_archive_confirmed": exc.archive_confirmed,
                        "status_archive_path": (
                            str(exc.archive_path) if exc.archive_path is not None else None
                        ),
                        "latest_status_state": "unknown",
                        "message": "每日运行状态未完整写入；不能据此判断本次业务操作未发生。",
                        "next_action": (
                            "先检查已有状态归档和 update_summary.json，修复磁盘写入问题，"
                            "再运行 paper-status 审计账本；确认后再决定是否重跑每日任务。"
                        ),
                    }
                )
                return 1
            _print_json(automation.payload)
            if automation.succeeded:
                print(f"每日模拟盘自动运行完成：{automation.latest_path}")
            else:
                print(
                    f"每日模拟盘自动运行失败：{automation.payload['failed_step']}",
                    file=sys.stderr,
                )
            return automation.exit_code
        if args.command == "account":
            try:
                report, account_payload = account_backtest_symbol(
                    args.symbol,
                    strategy=args.strategy,
                    short_window=args.short,
                    long_window=args.long,
                    config=_ledger_config(args),
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"账户账本报告已生成：{report}")
            _print_json(account_payload)
            return 0
        if args.command == "account-all":
            account_reports = batch_progress
            for instrument in load_instruments(get_paths().root):
                batch_current_symbol = instrument.symbol
                if instrument.kind == "index":
                    account_reports.append(
                        {
                            "symbol": instrument.symbol,
                            "status": "skipped",
                            "reason": "指数本身不可直接交易",
                        }
                    )
                    continue
                report, account_payload = account_backtest_symbol(
                    instrument.symbol,
                    instrument_kind=instrument.kind,
                    strategy=args.strategy,
                    short_window=args.short,
                    long_window=args.long,
                    config=_ledger_config(args),
                )
                account_reports.append(
                    {
                        "symbol": instrument.symbol,
                        "report": str(report),
                        **account_payload,
                    }
                )
            _print_batch_results("账户账本报告已生成", account_reports)
            return 0
        if args.command == "fetch":
            try:
                payload = update_market_data(args.start, args.end)
            except (OSError, duckdb.Error, MarketDataStateError):
                return _market_update_interrupted()
            _print_json(payload)
            if payload["status"] == "failed":
                return 2
            if payload["status"] == "partial_sources":
                return 3
            return 0
        if args.command == "backtest":
            try:
                report, metrics = backtest_symbol(
                    args.symbol,
                    strategy=args.strategy,
                    short_window=args.short,
                    long_window=args.long,
                    momentum_lookback=args.momentum_lookback,
                    cost_bps=args.cost_bps,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"回测报告已生成：{report}")
            _print_json(metrics)
            return 0
        if args.command == "experiment":
            try:
                report, payload = experiment_symbol(
                    args.symbol,
                    split_date=args.split_date,
                    strategy=args.strategy,
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"样本外实验报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "experiment-all":
            reports = batch_progress
            for instrument in load_instruments(get_paths().root):
                batch_current_symbol = instrument.symbol
                report, payload = experiment_symbol(
                    instrument.symbol,
                    split_date=args.split_date,
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
            _print_batch_results("样本外实验报告已生成", reports)
            return 0
        if args.command == "walk-forward":
            try:
                report, payload = walk_forward_symbol(
                    args.symbol,
                    first_oos_date=args.first_oos_date,
                    fold_months=args.fold_months,
                    strategy=args.strategy,
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"滚动检验报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "walk-forward-all":
            reports = batch_progress
            for instrument in load_instruments(get_paths().root):
                batch_current_symbol = instrument.symbol
                report, payload = walk_forward_symbol(
                    instrument.symbol,
                    first_oos_date=args.first_oos_date,
                    fold_months=args.fold_months,
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
            _print_batch_results("滚动检验报告已生成", reports)
            return 0
        if args.command == "cost-stress":
            try:
                report, payload = cost_stress_symbol(
                    args.symbol,
                    first_oos_date=args.first_oos_date,
                    cost_scenarios_bps=args.costs,
                    fold_months=args.fold_months,
                    short_window=args.short,
                    long_window=args.long,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"成本压力报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "cost-stress-all":
            reports = batch_progress
            for instrument in load_instruments(get_paths().root):
                batch_current_symbol = instrument.symbol
                report, payload = cost_stress_symbol(
                    instrument.symbol,
                    first_oos_date=args.first_oos_date,
                    cost_scenarios_bps=args.costs,
                    fold_months=args.fold_months,
                    short_window=args.short,
                    long_window=args.long,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
            _print_batch_results("成本压力报告已生成", reports)
            return 0
        if args.command == "parameter-test":
            try:
                report, payload = parameter_test_symbol(
                    args.symbol,
                    first_oos_date=args.first_oos_date,
                    short_windows=args.shorts,
                    long_windows=args.longs,
                    reference_pair=(args.reference_short, args.reference_long),
                    fold_months=args.fold_months,
                    cost_bps=args.cost_bps,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"参数稳健性报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "parameter-test-all":
            reports = batch_progress
            for instrument in load_instruments(get_paths().root):
                batch_current_symbol = instrument.symbol
                report, payload = parameter_test_symbol(
                    instrument.symbol,
                    first_oos_date=args.first_oos_date,
                    short_windows=args.shorts,
                    long_windows=args.longs,
                    reference_pair=(args.reference_short, args.reference_long),
                    fold_months=args.fold_months,
                    cost_bps=args.cost_bps,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
            _print_batch_results("参数稳健性报告已生成", reports)
            return 0
        if args.command == "execution-test":
            instruments = load_instruments(get_paths().root)
            selected_instrument = next(
                (item for item in instruments if item.symbol == args.symbol),
                None,
            )
            try:
                report, payload = execution_test_symbol(
                    args.symbol,
                    first_oos_date=args.first_oos_date,
                    instrument_kind=selected_instrument.kind if selected_instrument else "unknown",
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                    forced_delay_days=args.delay_days,
                    lock_threshold_pct=args.lock_threshold,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"执行可行性压力测试报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "execution-test-all":
            reports = batch_progress
            for instrument in load_instruments(get_paths().root):
                batch_current_symbol = instrument.symbol
                report, payload = execution_test_symbol(
                    instrument.symbol,
                    first_oos_date=args.first_oos_date,
                    instrument_kind=instrument.kind,
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                    forced_delay_days=args.delay_days,
                    lock_threshold_pct=args.lock_threshold,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
            _print_batch_results("执行可行性压力测试报告已生成", reports)
            return 0
        if args.command == "compare":
            try:
                report, payload = strategy_compare_symbol(
                    args.symbol,
                    split_date=args.split_date,
                    short_window=args.short,
                    long_window=args.long,
                    momentum_lookback=args.momentum_lookback,
                    cost_bps=args.cost_bps,
                )
            except ReportWriteError:
                return _report_write_failure()
            print(f"多策略对比报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "compare-all":
            reports = batch_progress
            for instrument in load_instruments(get_paths().root):
                batch_current_symbol = instrument.symbol
                report, payload = strategy_compare_symbol(
                    instrument.symbol,
                    split_date=args.split_date,
                    short_window=args.short,
                    long_window=args.long,
                    momentum_lookback=args.momentum_lookback,
                    cost_bps=args.cost_bps,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
            _print_batch_results("多策略对比报告已生成", reports)
            return 0
        if args.command == "validate":
            payload = validate_curated_data()
            _print_json(payload)
            return 0 if payload["status"] == "passed" else 3
        if args.command == "report-verify":
            payload = verify_report_bundle(args.report.expanduser().absolute())
            _print_json(payload)
            return 0 if payload["ok"] else 3
        if args.command == "all":
            try:
                payload = update_market_data(args.start, args.end)
            except (OSError, duckdb.Error, MarketDataStateError):
                return _market_update_interrupted(includes_reports=True)
            if int(payload["successful_instruments"]) == 0:
                _print_json(payload)
                return 2
            if payload["status"] == "partial_sources":
                _print_json(payload)
                return 3
            confirmed_reports: list[dict[str, str]] = []
            for instrument in load_instruments(get_paths().root):
                record = next(
                    item for item in payload["records"] if item["symbol"] == instrument.symbol
                )
                if record["rows"] > 0:
                    try:
                        report, _ = backtest_symbol(instrument.symbol)
                    except ReportWriteError:
                        _print_json(
                            {
                                "ok": False,
                                "status": "failed",
                                "error_code": "report_write_failed_after_data_commit",
                                "stage": "write_report",
                                "data_committed": True,
                                "market_update_status": payload["status"],
                                "report_batch_complete": False,
                                "reports_confirmed": confirmed_reports,
                                "failed_symbol": instrument.symbol,
                                "current_report_state": "unknown",
                                "message": (
                                    "行情更新已完成，但回测报告未全部生成；"
                                    "当前标的可能留下部分输出文件。"
                                ),
                                "next_action": (
                                    "保留已更新行情，先检查输出目录和磁盘权限；"
                                    "修复后核验已生成报告，再单独运行 backtest 补齐失败标的。"
                                ),
                            }
                        )
                        return 1
                    confirmed_reports.append({"symbol": instrument.symbol, "report": str(report)})
            _print_json(payload)
            for item in confirmed_reports:
                print(f"回测报告已生成：{item['report']}")
            return 0
    except PaperPortfolioNotFound as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return PaperPortfolioNotFound.exit_code
    except Exception as exc:
        if args.command in {
            "account-all",
            "experiment-all",
            "walk-forward-all",
            "cost-stress-all",
            "parameter-test-all",
            "execution-test-all",
            "compare-all",
        }:
            _print_json(
                {
                    "ok": False,
                    "error_code": "batch_report_failed",
                    "stage": "generate_reports",
                    "data_committed": False,
                    "report_batch_complete": False,
                    "reports_confirmed": [
                        {"symbol": item["symbol"], "report": item["report"]}
                        for item in batch_progress
                        if "report" in item
                    ],
                    "failed_symbol": batch_current_symbol,
                    "current_report_state": "unknown",
                    "message": "批量报告未完成；已确认的报告保留，当前标的可能留下部分文件。",
                    "next_action": (
                        "先检查已确认报告和当前标的的输出文件；不要将本次运行视为完整批次，"
                        "确认失败原因后再重试。"
                    ),
                }
            )
            return 1
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
