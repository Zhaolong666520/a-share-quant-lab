from __future__ import annotations

import html
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from finance_lab.config import ProjectPaths
from finance_lab.ledger import AccountLedgerResult


def _safe_symbol(symbol: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", symbol).strip("_")
    return safe or "instrument"


def _percent(value: float) -> str:
    return f"{value:.2%}"


def write_account_ledger_report(
    result: AccountLedgerResult,
    paths: ProjectPaths,
) -> tuple[Path, Path, Path, Path, Path]:
    stem = f"{_safe_symbol(result.symbol)}_{result.strategy}_account_{result.experiment_id}"
    html_path = paths.outputs / f"{stem}_report.html"
    json_path = paths.outputs / f"{stem}_metrics.json"
    csv_path = paths.outputs / f"{stem}_trades.csv"
    daily_csv_path = paths.outputs / f"{stem}_daily_ledger.csv"
    chart_path = paths.outputs / f"{stem}_equity.png"
    result.trades.to_csv(csv_path, index=False, encoding="utf-8-sig")
    result.frame.to_csv(daily_csv_path, index=False, encoding="utf-8-sig")

    figure, axis = plt.subplots(figsize=(10, 5.6))
    axis.plot(result.frame["return_end_date"], result.frame["equity"], label="Strategy")
    axis.plot(
        result.benchmark_frame["return_end_date"],
        result.benchmark_frame["equity"],
        label="Buy & hold",
        alpha=0.85,
    )
    axis.set_title(f"{result.symbol} - cash and lot ledger")
    axis.set_ylabel("Account equity (initial = 1.0)")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)

    config = result.config
    if result.data_health:
        health = result.data_health
        stale_days = (
            health.business_days_stale
            if health.business_days_stale is not None
            else "未知"
        )
        health_details = html.escape(
            json.dumps(health.upstream_errors, ensure_ascii=False)
            if health.upstream_errors
            else "无"
        )
        health_block = f"""<p class="warning"><strong>数据健康状态：</strong>
清单 {html.escape(health.manifest_health_status)}；本文件 {html.escape(health.file_health_status)}；
检查日 {health.as_of_date}；数据区间 {health.start_date or '未知'} 至 {health.end_date or '未知'}；
相对检查日滞后交易日 {stale_days}；
来源 {html.escape(str(health.sources))}；提醒 {html.escape(str(health.warning_codes))}；
当前标的上游错误 {health_details}。</p>"""
    else:
        health_block = (
            '<p class="warning"><strong>数据健康状态：</strong>未由正式流水线提供清单快照。</p>'
        )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(result.symbol)} 账户账本回测</title><style>
body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 1050px; margin: 36px auto;
padding: 0 20px; color: #172033; line-height: 1.65; }}
.warning {{ background: #fff4d6; border-left: 5px solid #ef9f27; padding: 12px 16px; }}
.note {{ background: #eef6ff; border-left: 5px solid #4a83c6; padding: 12px 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
td,th {{ padding: 9px; border-bottom: 1px solid #ddd; }}
img {{ max-width: 100%; border: 1px solid #ddd; }} code {{ background: #f2f4f7; padding: 2px 5px; }}
</style></head><body>
<h1>{html.escape(result.symbol)} 账户账本回测</h1>
<p class="warning"><strong>假设费用与成交：</strong>佣金、最低收费、卖出税费和滑点均为教学参数，
不代表当前券商或法定标准。模型假设信号都能在次日开盘成交，尚未处理分红、除权现金流、停牌、
涨跌停阻塞和部分成交，不构成投资建议。</p>
{health_block}
<p>初始资金 <strong>{config.initial_cash:,.2f}</strong> 元；每手 {config.lot_size} 份；佣金
{config.commission_bps!r} bps（最低 {config.minimum_commission:,.2f} 元）；卖出税费
{config.sell_tax_bps!r} bps；单边滑点 {config.slippage_bps!r} bps。</p>
<table><thead><tr><th>指标</th><th>策略</th><th>买入持有</th></tr></thead><tbody>
<tr><td>累计收益</td><td>{_percent(result.metrics.total_return)}</td>
<td>{_percent(result.benchmark_metrics.total_return)}</td></tr>
<tr><td>最大回撤</td><td>{_percent(result.metrics.max_drawdown)}</td>
<td>{_percent(result.benchmark_metrics.max_drawdown)}</td></tr>
<tr><td>交易边数</td><td>{result.metrics.trade_sides}</td>
<td>{result.benchmark_metrics.trade_sides}</td></tr>
<tr><td>期末权益</td><td>{result.final_equity:,.2f}</td>
<td>{result.benchmark_final_equity:,.2f}</td></tr>
</tbody></table>
<p class="note">策略账本检查：<strong>{'通过' if result.checks.passed else '失败'}</strong>；
基准账本检查：<strong>{'通过' if result.benchmark_checks.passed else '失败'}</strong>。</p>
<img src="{html.escape(chart_path.name)}" alt="账户净值图">
<p>实验ID：<code>{html.escape(result.experiment_id)}</code>；规范行语义指纹SHA-256：
<code>{html.escape(result.data_fingerprint)}</code>。</p>
<p>数据集ID：<code>{html.escape(result.dataset_id or '未由正式流水线提供')}</code>；Parquet文件
SHA-256：<code>{html.escape(result.curated_file_sha256 or '未由正式流水线提供')}</code>。</p>
<p><a href="{html.escape(csv_path.name)}">打开逐笔成交账本CSV</a>；
<a href="{html.escape(daily_csv_path.name)}">打开逐日账户账本CSV</a></p>
</body></html>"""
    html_path.write_text(document, encoding="utf-8")
    payload = {
        "settings": result.settings_dict(),
        "metrics": result.metrics.to_dict(),
        "benchmark_metrics": result.benchmark_metrics.to_dict(),
        "checks": result.checks.to_dict(),
        "benchmark_checks": result.benchmark_checks.to_dict(),
        "trade_count": len(result.trades),
        "final_equity": result.final_equity,
        "benchmark_final_equity": result.benchmark_final_equity,
        "limitations": [
            "All fee and slippage inputs are hypothetical scenarios.",
            "Dividend and corporate-action cash flows are not yet modeled.",
            "Partial fills, order queues, and market impact are not modeled.",
            "Suspensions and price-limit execution blocks are not modeled.",
            "Historical results are not investment advice.",
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return html_path, json_path, csv_path, daily_csv_path, chart_path
