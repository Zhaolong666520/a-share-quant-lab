from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from finance_lab.config import get_paths, load_instruments
from finance_lab.ledger import LedgerConfig
from finance_lab.paper_automation import run_paper_daily_automation
from finance_lab.paper_pipeline import (
    PaperPortfolioNotFound,
    paper_init_portfolio,
    paper_run_portfolio,
    paper_status_portfolio,
)
from finance_lab.pipeline import (
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


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD") from exc


def _parse_costs(value: str) -> tuple[float, ...]:
    try:
        costs = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("成本必须是逗号分隔的数字，例如 5,10,20,50") from exc
    if not costs:
        raise argparse.ArgumentTypeError("至少需要一个成本数字")
    return costs


def _parse_windows(value: str) -> tuple[int, ...]:
    try:
        windows = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("窗口必须是逗号分隔的整数，例如 10,20,30") from exc
    if not windows:
        raise argparse.ArgumentTypeError("至少需要一个窗口数字")
    return windows


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


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

    manifest = subparsers.add_parser("manifest", help="生成本地数据集指纹与健康报告")
    manifest.add_argument("--as-of", type=_parse_date, default=date.today())
    manifest.add_argument("--stale-after-business-days", type=int, default=3)

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
    backtest.add_argument(
        "--strategy", choices=["sma", "buy_hold", "momentum"], default="sma"
    )
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
    execution_all.add_argument(
        "--first-oos-date", type=_parse_date, default=date(2021, 1, 1)
    )
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

    all_command = subparsers.add_parser("all", help="更新数据并回测全部配置标的")
    all_command.add_argument("--start", type=_parse_date, default=date(2018, 1, 1))
    all_command.add_argument("--end", type=_parse_date, default=date.today())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            report, metrics = create_demo_report()
            print(f"离线演示已生成：{report}")
            _print_json(metrics)
            return 0
        if args.command == "manifest":
            report, manifest_payload = data_health(
                as_of_date=args.as_of,
                stale_after_business_days=args.stale_after_business_days,
            )
            print(f"数据集健康报告已生成：{report}")
            _print_json(manifest_payload)
            return 0 if manifest_payload["health_status"] != "error" else 3
        if args.command == "paper-init":
            result = paper_init_portfolio(
                portfolio_id=args.portfolio,
                ledger_config=_ledger_config(args),
                as_of_date=args.as_of,
                stale_after_business_days=args.stale_after_business_days,
            )
            if result.report_path:
                print(f"模拟盘报告已生成：{result.report_path}")
            _print_json(_paper_operation_payload(result))
            return 0
        if args.command == "paper-run":
            result = paper_run_portfolio(
                portfolio_id=args.portfolio,
                as_of_date=args.as_of,
                stale_after_business_days=args.stale_after_business_days,
            )
            if result.report_path:
                print(f"模拟盘报告已生成：{result.report_path}")
            _print_json(_paper_operation_payload(result))
            return 0
        if args.command == "paper-status":
            result = paper_status_portfolio(portfolio_id=args.portfolio)
            if result.report_path:
                print(f"模拟盘报告已生成：{result.report_path}")
            _print_json(_paper_operation_payload(result))
            return 0
        if args.command == "paper-daily":
            automation = run_paper_daily_automation(
                portfolio_id=args.portfolio,
                start_date=args.start,
                as_of_date=args.as_of,
                stale_after_business_days=args.stale_after_business_days,
            )
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
            report, account_payload = account_backtest_symbol(
                args.symbol,
                strategy=args.strategy,
                short_window=args.short,
                long_window=args.long,
                config=_ledger_config(args),
            )
            print(f"账户账本报告已生成：{report}")
            _print_json(account_payload)
            return 0
        if args.command == "account-all":
            account_reports: list[dict[str, object]] = []
            for instrument in load_instruments(get_paths().root):
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
                print(f"账户账本报告已生成：{report}")
            _print_json(account_reports)
            return 0
        if args.command == "fetch":
            payload = update_market_data(args.start, args.end)
            _print_json(payload)
            return 0 if int(payload["successful_instruments"]) > 0 else 2
        if args.command == "backtest":
            report, metrics = backtest_symbol(
                args.symbol,
                strategy=args.strategy,
                short_window=args.short,
                long_window=args.long,
                momentum_lookback=args.momentum_lookback,
                cost_bps=args.cost_bps,
            )
            print(f"回测报告已生成：{report}")
            _print_json(metrics)
            return 0
        if args.command == "experiment":
            report, payload = experiment_symbol(
                args.symbol,
                split_date=args.split_date,
                strategy=args.strategy,
                short_window=args.short,
                long_window=args.long,
                cost_bps=args.cost_bps,
            )
            print(f"样本外实验报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "experiment-all":
            reports = []
            for instrument in load_instruments(get_paths().root):
                report, payload = experiment_symbol(
                    instrument.symbol,
                    split_date=args.split_date,
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
                print(f"样本外实验报告已生成：{report}")
            _print_json(reports)
            return 0
        if args.command == "walk-forward":
            report, payload = walk_forward_symbol(
                args.symbol,
                first_oos_date=args.first_oos_date,
                fold_months=args.fold_months,
                strategy=args.strategy,
                short_window=args.short,
                long_window=args.long,
                cost_bps=args.cost_bps,
            )
            print(f"滚动检验报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "walk-forward-all":
            reports = []
            for instrument in load_instruments(get_paths().root):
                report, payload = walk_forward_symbol(
                    instrument.symbol,
                    first_oos_date=args.first_oos_date,
                    fold_months=args.fold_months,
                    short_window=args.short,
                    long_window=args.long,
                    cost_bps=args.cost_bps,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
                print(f"滚动检验报告已生成：{report}")
            _print_json(reports)
            return 0
        if args.command == "cost-stress":
            report, payload = cost_stress_symbol(
                args.symbol,
                first_oos_date=args.first_oos_date,
                cost_scenarios_bps=args.costs,
                fold_months=args.fold_months,
                short_window=args.short,
                long_window=args.long,
            )
            print(f"成本压力报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "cost-stress-all":
            reports = []
            for instrument in load_instruments(get_paths().root):
                report, payload = cost_stress_symbol(
                    instrument.symbol,
                    first_oos_date=args.first_oos_date,
                    cost_scenarios_bps=args.costs,
                    fold_months=args.fold_months,
                    short_window=args.short,
                    long_window=args.long,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
                print(f"成本压力报告已生成：{report}")
            _print_json(reports)
            return 0
        if args.command == "parameter-test":
            report, payload = parameter_test_symbol(
                args.symbol,
                first_oos_date=args.first_oos_date,
                short_windows=args.shorts,
                long_windows=args.longs,
                reference_pair=(args.reference_short, args.reference_long),
                fold_months=args.fold_months,
                cost_bps=args.cost_bps,
            )
            print(f"参数稳健性报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "parameter-test-all":
            reports = []
            for instrument in load_instruments(get_paths().root):
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
                print(f"参数稳健性报告已生成：{report}")
            _print_json(reports)
            return 0
        if args.command == "execution-test":
            instruments = load_instruments(get_paths().root)
            selected_instrument = next(
                (item for item in instruments if item.symbol == args.symbol),
                None,
            )
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
            print(f"执行可行性压力测试报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "execution-test-all":
            reports = []
            for instrument in load_instruments(get_paths().root):
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
                print(f"执行可行性压力测试报告已生成：{report}")
            _print_json(reports)
            return 0
        if args.command == "compare":
            report, payload = strategy_compare_symbol(
                args.symbol,
                split_date=args.split_date,
                short_window=args.short,
                long_window=args.long,
                momentum_lookback=args.momentum_lookback,
                cost_bps=args.cost_bps,
            )
            print(f"多策略对比报告已生成：{report}")
            _print_json(payload)
            return 0
        if args.command == "compare-all":
            reports = []
            for instrument in load_instruments(get_paths().root):
                report, payload = strategy_compare_symbol(
                    instrument.symbol,
                    split_date=args.split_date,
                    short_window=args.short,
                    long_window=args.long,
                    momentum_lookback=args.momentum_lookback,
                    cost_bps=args.cost_bps,
                )
                reports.append({"symbol": instrument.symbol, "report": str(report), **payload})
                print(f"多策略对比报告已生成：{report}")
            _print_json(reports)
            return 0
        if args.command == "validate":
            payload = validate_curated_data()
            _print_json(payload)
            return 0 if int(payload["error_count"]) == 0 else 3
        if args.command == "all":
            payload = update_market_data(args.start, args.end)
            _print_json(payload)
            if int(payload["successful_instruments"]) == 0:
                return 2
            for instrument in load_instruments(get_paths().root):
                record = next(
                    item for item in payload["records"] if item["symbol"] == instrument.symbol
                )
                if record["rows"] > 0:
                    report, _ = backtest_symbol(instrument.symbol)
                    print(f"回测报告已生成：{report}")
            return 0
    except PaperPortfolioNotFound as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return PaperPortfolioNotFound.exit_code
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
