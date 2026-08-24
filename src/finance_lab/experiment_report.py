from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd

from finance_lab import __version__
from finance_lab.backtest import BacktestMetrics
from finance_lab.config import ProjectPaths
from finance_lab.experiment import SplitExperimentResult


def _safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _period_cells(metrics: BacktestMetrics) -> list[str]:
    return [
        _percent(metrics.total_return),
        _percent(metrics.annualized_return),
        _percent(metrics.annualized_volatility),
        f"{metrics.sharpe_ratio:.2f}",
        _percent(metrics.max_drawdown),
        str(metrics.trade_sides),
        _percent(metrics.exposure),
    ]


def _metrics_table(result: SplitExperimentResult) -> str:
    labels = ["累计收益", "年化收益", "年化波动", "夏普比率", "最大回撤", "交易边数", "持仓比例"]
    columns = [
        _period_cells(result.development.metrics),
        _period_cells(result.development.benchmark_metrics),
        _period_cells(result.out_of_sample.metrics),
        _period_cells(result.out_of_sample.benchmark_metrics),
    ]
    rows = []
    for index, label in enumerate(labels):
        cells = "".join(f"<td>{column[index]}</td>" for column in columns)
        rows.append(f"<tr><td>{label}</td>{cells}</tr>")
    return "\n".join(rows)


def _observation(result: SplitExperimentResult) -> str:
    development = result.development.metrics.annualized_return
    out_of_sample = result.out_of_sample.metrics.annualized_return
    if out_of_sample < development:
        return "样本外年化收益低于开发期，说明历史表现存在退化，不能仅凭开发期结果下结论。"
    return "样本外年化收益没有低于开发期，但仍需更多市场阶段和现实交易约束验证。"


def write_split_experiment_report(
    result: SplitExperimentResult,
    paths: ProjectPaths,
) -> tuple[Path, Path, Path, Path]:
    stem = f"{_safe_symbol(result.symbol)}_{result.strategy}_split_{result.experiment_id}"
    chart_path = paths.outputs / f"{stem}_equity.png"
    html_path = paths.outputs / f"{stem}_report.html"
    json_path = paths.outputs / f"{stem}_metrics.json"
    trades_path = paths.outputs / f"{stem}_trades.csv"

    frame = result.full_result.frame
    split_timestamp = pd.Timestamp(result.split_date)
    figure, axis = plt.subplots(figsize=(11, 5.8))
    axis.plot(frame["trade_date"], frame["equity"], label="Strategy", lw=1.8)
    axis.plot(
        frame["trade_date"],
        frame["benchmark_equity"],
        label="Buy & Hold",
        lw=1.3,
        alpha=0.8,
    )
    axis.axvline(split_timestamp, color="#cc3d3d", linestyle="--", label="OOS starts")
    axis.axvspan(
        split_timestamp,
        frame["trade_date"].max(),
        color="#ffcc66",
        alpha=0.12,
    )
    axis.set_title(f"{result.symbol} - fixed development / out-of-sample split")
    axis.set_ylabel("Equity (start = 1.0)")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)

    trades = result.trades.copy()
    trades["trade_date"] = trades["trade_date"].dt.strftime("%Y-%m-%d")
    trades["signal_date"] = trades["signal_date"].dt.strftime("%Y-%m-%d")
    trades.to_csv(trades_path, index=False, encoding="utf-8-sig")

    sources = ", ".join(sorted(str(value) for value in frame["source"].dropna().unique()))
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result.symbol)} 样本外实验报告</title>
  <style>
    body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 1120px;
           margin: 36px auto; padding: 0 20px; color: #172033; line-height: 1.65; }}
    .warning {{ background: #fff4d6; border-left: 5px solid #ef9f27; padding: 12px 16px; }}
    .note {{ background: #eef6ff; border-left: 5px solid #4a83c6; padding: 12px 16px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 14px; }}
    th, td {{ padding: 9px; border-bottom: 1px solid #dfe4ea; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    img {{ max-width: 100%; border: 1px solid #e3e8ef; }}
    code {{ background: #f2f4f7; padding: 2px 5px; }}
  </style>
</head>
<body>
  <h1>{html.escape(result.symbol)} 固定切分实验</h1>
  <p class="warning"><strong>仅用于学习：</strong>历史结果不代表未来收益，不构成投资建议。</p>
  <p>开发期：{result.development.start_date} 至 {result.development.end_date}；
     样本外期：{result.out_of_sample.start_date} 至 {result.out_of_sample.end_date}。</p>
  <p>固定参数：短均线 <code>{result.short_window}</code>，长均线
     <code>{result.long_window}</code>，单边成本 <code>{result.cost_bps:.1f} bps</code>，
     数据来源 <code>{html.escape(sources)}</code>。</p>
  <p>实验ID：<code>{html.escape(result.experiment_id)}</code>；引擎版本：
     <code>{html.escape(__version__)}</code>；数据SHA-256：
     <code>{html.escape(result.data_fingerprint)}</code>。</p>
  <p class="note"><strong>本次观察：</strong>{html.escape(_observation(result))}</p>
  <table>
    <thead><tr><th>指标</th><th>开发期策略</th><th>开发期基准</th>
      <th>样本外策略</th><th>样本外基准</th></tr></thead>
    <tbody>{_metrics_table(result)}</tbody>
  </table>
  <img src="{html.escape(chart_path.name)}" alt="样本外切分净值曲线">
  <h2>切分规则</h2>
  <p>参数没有根据样本外结果自动调整。样本外期可以使用开发期价格作为均线预热，并延续
     已存在的仓位；这模拟策略从开发阶段连续运行，而不是在切分日凭空重启。</p>
  <h2>交易流水</h2>
  <p>共记录 {len(trades)} 个买卖边。CSV中的成本是净值比例估算，不是实际人民币金额：
     <a href="{html.escape(trades_path.name)}">打开交易流水</a>。</p>
</body>
</html>
"""
    html_path.write_text(document, encoding="utf-8")
    payload = {
        "settings": result.settings_dict(),
        "full_metrics": result.full_result.metrics.to_dict(),
        "full_benchmark_metrics": result.full_result.benchmark_metrics.to_dict(),
        "development": result.development.to_dict(),
        "out_of_sample": result.out_of_sample.to_dict(),
        "trade_rows": len(trades),
        "notes": [
            "Parameters were not tuned on the out-of-sample period.",
            "The out-of-sample period uses prior prices for indicator warm-up.",
            "Historical results are not investment advice.",
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return html_path, chart_path, json_path, trades_path
