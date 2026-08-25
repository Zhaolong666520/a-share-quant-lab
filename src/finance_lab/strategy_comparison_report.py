from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd

from finance_lab.config import ProjectPaths
from finance_lab.strategy_comparison import StrategyComparisonResult


def _safe_symbol(symbol: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", symbol).strip("_")
    return safe or "instrument"


def _output_path(paths: ProjectPaths, filename: str) -> Path:
    output_root = paths.outputs.resolve()
    candidate = (output_root / filename).resolve()
    if not candidate.is_relative_to(output_root):
        raise ValueError("报告输出路径越过 outputs 目录")
    return candidate


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _parameter_label(strategy: str, parameters: dict[str, int]) -> str:
    if strategy == "sma":
        return f"{parameters['short_window']}/{parameters['long_window']} 日"
    if strategy == "momentum":
        return f"{parameters['lookback']} 日"
    return "固定持有"


def _report_context_id(result: StrategyComparisonResult) -> str:
    payload = {
        "dataset_id": result.dataset_id,
        "curated_file_sha256": result.curated_file_sha256,
        "manifest_as_of_date": (
            result.manifest_as_of_date.isoformat() if result.manifest_as_of_date else None
        ),
        "business_days_stale": result.business_days_stale,
        "data_health_status": result.data_health_status,
        "manifest_health_status": result.manifest_health_status,
        "data_warning_codes": result.data_warning_codes,
        "upstream_errors": result.upstream_errors,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:8]


def _summary_rows(result: StrategyComparisonResult) -> tuple[list[dict[str, object]], str]:
    records: list[dict[str, object]] = []
    html_rows: list[str] = []
    benchmark_return = result.benchmark_metrics.total_return
    for scenario in result.scenarios:
        metrics = scenario.out_of_sample_metrics
        record = {
            "strategy": scenario.strategy,
            "label": scenario.label,
            "parameters": _parameter_label(scenario.strategy, scenario.parameters),
            "development_total_return": scenario.development_metrics.total_return,
            "out_of_sample_total_return": metrics.total_return,
            "out_of_sample_annualized_return": metrics.annualized_return,
            "out_of_sample_max_drawdown": metrics.max_drawdown,
            "out_of_sample_sharpe": metrics.sharpe_ratio,
            "out_of_sample_trade_sides": metrics.trade_sides,
            "out_of_sample_exposure": metrics.exposure,
            "return_difference_vs_buy_hold": metrics.total_return - benchmark_return,
        }
        records.append(record)
        html_rows.append(
            "<tr>"
            f"<td>{html.escape(scenario.label)}</td>"
            f"<td>{html.escape(str(record['parameters']))}</td>"
            f"<td>{_percent(scenario.development_metrics.total_return)}</td>"
            f"<td>{_percent(metrics.total_return)}</td>"
            f"<td>{_percent(metrics.annualized_return)}</td>"
            f"<td>{_percent(metrics.max_drawdown)}</td>"
            f"<td>{metrics.sharpe_ratio:.2f}</td>"
            f"<td>{metrics.trade_sides}</td>"
            f"<td>{_percent(metrics.exposure)}</td>"
            f"<td>{record['return_difference_vs_buy_hold']:+.2%}</td>"
            "</tr>"
        )
    return records, "\n".join(html_rows)


def write_strategy_comparison_report(
    result: StrategyComparisonResult,
    paths: ProjectPaths,
) -> tuple[Path, Path, Path, Path]:
    stem = (
        f"{_safe_symbol(result.symbol)}_strategy_compare_{result.experiment_id}_"
        f"r{_report_context_id(result)}"
    )
    html_path = _output_path(paths, f"{stem}_report.html")
    chart_path = _output_path(paths, f"{stem}_equity.png")
    json_path = _output_path(paths, f"{stem}_metrics.json")
    csv_path = _output_path(paths, f"{stem}_summary.csv")

    figure, axis = plt.subplots(figsize=(11, 5.8))
    for scenario in result.scenarios:
        frame = scenario.out_of_sample_frame
        chart_label = (
            "Buy & Hold"
            if scenario.strategy == "buy_hold"
            else f"SMA {result.short_window}/{result.long_window}"
            if scenario.strategy == "sma"
            else f"Momentum {result.momentum_lookback}d"
        )
        axis.plot(
            frame["return_end_date"],
            frame["period_equity"],
            label=chart_label,
            lw=1.8,
        )
    axis.set_title(f"{result.symbol} - fixed strategy comparison (OOS)")
    axis.set_ylabel("Equity (OOS start = 1.0)")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)

    records, table_rows = _summary_rows(result)
    pd.DataFrame(records).to_csv(csv_path, index=False, encoding="utf-8-sig")
    upstream_text = html.escape(
        json.dumps(result.upstream_errors, ensure_ascii=False, sort_keys=True)
    )
    lineage = (
        f"数据集ID：<code>{html.escape(result.dataset_id)}</code>；文件SHA-256："
        f"<code>{html.escape(result.curated_file_sha256)}</code>；数据区间："
        f"<code>{result.data_start_date or '未知'} 至 {result.data_end_date or '未知'}</code>；"
        f"来源：<code>{html.escape(str(result.data_sources))}</code>；复权："
        f"<code>{html.escape(str(result.data_adjustments))}</code>；成交量单位："
        f"<code>{html.escape(str(result.data_volume_units))}</code>；文件健康状态："
        f"<code>{html.escape(result.data_health_status)}</code>；数据集健康状态："
        f"<code>{html.escape(result.manifest_health_status or '未知')}</code>；检查日："
        f"<code>{result.manifest_as_of_date or '未知'}</code>；近似滞后工作日："
        "<code>"
        f"{result.business_days_stale if result.business_days_stale is not None else '未知'}"
        "</code>；"
        f"提醒代码：<code>{html.escape(str(result.data_warning_codes))}</code>；"
        f"上游失败：<code>{upstream_text}</code>。"
        if result.dataset_id and result.curated_file_sha256 and result.data_health_status
        else "本次通过Python API直接运行；请结合数据清单确认正式数据身份。"
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result.symbol)} 多策略对比报告</title>
  <style>
    body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 1180px;
           margin: 36px auto; padding: 0 20px; color: #172033; line-height: 1.65; }}
    .warning {{ background: #fff4d6; border-left: 5px solid #ef9f27; padding: 12px 16px; }}
    .note {{ background: #eef6ff; border-left: 5px solid #4a83c6; padding: 12px 16px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }}
    th, td {{ padding: 9px; border-bottom: 1px solid #dfe4ea; text-align: right; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    img {{ max-width: 100%; border: 1px solid #e3e8ef; }}
    code {{ background: #f2f4f7; padding: 2px 5px; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <h1>{html.escape(result.symbol)} 固定多策略对比</h1>
  <p class="warning"><strong>仅用于学习：</strong>这是已观察历史区间的回顾性诊断，
     不构成投资建议，也不代表未来表现。</p>
  <p>开发期收益实现日：{result.development_start} 至 {result.development_end}；样本外交易日从
     {result.out_of_sample_start} 开始，最后收益实现日为 {result.out_of_sample_end}；跨越切分日
     的收益行已排除。统一假设单边成本：<code>{result.cost_bps!r} bps</code>。</p>
  <p>固定策略：买入持有；{result.short_window}/{result.long_window} 日双均线；
     {result.momentum_lookback} 日时间序列动量。信号均在收盘后生成，最早于下一交易日开盘执行。</p>
  <p>实验ID：<code>{html.escape(result.experiment_id)}</code>；语义数据指纹：
     <code>{html.escape(result.data_fingerprint)}</code>。{lineage}</p>
  <p class="note"><strong>阅读原则：</strong>三种策略按预先固定顺序完整展示；程序不会根据
     这段历史自动挑选、推荐或替换任何策略参数。</p>
  <table>
    <thead><tr><th>策略</th><th>固定参数</th><th>开发期收益</th><th>样本外收益</th>
      <th>样本外年化</th><th>最大回撤</th><th>夏普</th><th>交易边数</th>
      <th>持仓比例</th><th>相对买入持有</th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
  <img src="{html.escape(chart_path.name)}" alt="固定策略样本外净值对比曲线">
  <h2>为什么不是选股器</h2>
  <p>对比的用途是检查规则在相同数据、时间边界和成本下如何不同，而不是从三条历史曲线中
     挑一条未来下注。若要提出新参数或新策略，应建立新的实验编号并预先写清假设。</p>
</body>
</html>
"""
    html_path.write_text(document, encoding="utf-8")
    payload = {
        "settings": result.settings_dict(),
        "benchmark_metrics": result.benchmark_metrics.to_dict(),
        "scenarios": [scenario.to_dict() for scenario in result.scenarios],
        "notes": [
            "All strategies use the same split, cost assumption, and benchmark returns.",
            "Strategy order is fixed; no historical winner is selected automatically.",
            "Signals execute no earlier than the next trading day's open.",
            "Historical results are not investment advice.",
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return html_path, chart_path, json_path, csv_path
