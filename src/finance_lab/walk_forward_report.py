from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np
import pandas as pd

from finance_lab.config import ProjectPaths
from finance_lab.walk_forward import WalkForwardFold, WalkForwardResult


def _safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _fold_row(fold: WalkForwardFold) -> dict[str, object]:
    return {
        "fold_index": fold.fold_index,
        "train_start": fold.train_start.isoformat(),
        "train_end": fold.train_end.isoformat(),
        "oos_start": fold.oos_start.isoformat(),
        "oos_end": fold.oos_end.isoformat(),
        "strategy_total_return": fold.metrics.total_return,
        "benchmark_total_return": fold.benchmark_metrics.total_return,
        "strategy_max_drawdown": fold.metrics.max_drawdown,
        "strategy_sharpe": fold.metrics.sharpe_ratio,
        "trade_sides": fold.metrics.trade_sides,
        "observations": fold.metrics.observations,
    }


def _fold_table(result: WalkForwardResult) -> str:
    rows = []
    for fold in result.folds:
        beats = fold.metrics.total_return > fold.benchmark_metrics.total_return
        rows.append(
            "<tr>"
            f"<td>{fold.fold_index}</td>"
            f"<td>{fold.oos_start} 至 {fold.oos_end}</td>"
            f"<td>{_percent(fold.metrics.total_return)}</td>"
            f"<td>{_percent(fold.benchmark_metrics.total_return)}</td>"
            f"<td>{_percent(fold.metrics.max_drawdown)}</td>"
            f"<td>{fold.metrics.sharpe_ratio:.2f}</td>"
            f"<td>{fold.metrics.trade_sides}</td>"
            f"<td>{'是' if beats else '否'}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _observation(result: WalkForwardResult) -> str:
    fold_count = len(result.folds)
    if result.beats_benchmark_folds * 2 <= fold_count:
        return "多数窗口未跑赢基准，当前证据不支持把该策略用于真实资金。"
    if result.positive_folds * 2 < fold_count:
        return "多数窗口收益为负，策略在不同市场阶段的稳定性不足。"
    return "部分稳定性指标尚可，但仍未纳入涨跌停、停牌、冲击成本和税费等完整约束。"


def _strategy_description(result: WalkForwardResult) -> str:
    if result.strategy == "sma":
        return f"固定使用{result.short_window}/{result.long_window}双均线"
    return "固定使用买入持有策略"


def write_walk_forward_report(
    result: WalkForwardResult,
    paths: ProjectPaths,
) -> tuple[Path, Path, Path, Path]:
    stem = f"{_safe_symbol(result.symbol)}_{result.strategy}_walk_{result.experiment_id}"
    html_path = paths.outputs / f"{stem}_report.html"
    chart_path = paths.outputs / f"{stem}_stability.png"
    json_path = paths.outputs / f"{stem}_metrics.json"
    csv_path = paths.outputs / f"{stem}_folds.csv"

    fold_rows = [_fold_row(fold) for fold in result.folds]
    pd.DataFrame(fold_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    aggregate = result.aggregate_frame.copy()
    aggregate["walk_equity"] = (1.0 + aggregate["net_return"]).cumprod()
    aggregate["benchmark_walk_equity"] = (1.0 + aggregate["benchmark_return"]).cumprod()
    labels = [f"F{fold.fold_index}" for fold in result.folds]
    positions = np.arange(len(result.folds))
    width = 0.38
    figure, axes = plt.subplots(2, 1, figsize=(11, 8), height_ratios=[1.5, 1.0])
    axes[0].plot(
        aggregate["return_end_date"],
        aggregate["walk_equity"],
        label="Strategy",
        lw=1.8,
    )
    axes[0].plot(
        aggregate["return_end_date"],
        aggregate["benchmark_walk_equity"],
        label="Buy & Hold",
        lw=1.4,
        alpha=0.85,
    )
    axes[0].set_title(f"{result.symbol} - walk-forward aggregate equity")
    axes[0].set_ylabel("Equity (start = 1.0)")
    axes[0].grid(alpha=0.25)
    axes[0].legend()
    axes[1].bar(
        positions - width / 2,
        [fold.metrics.total_return for fold in result.folds],
        width,
        label="Strategy",
    )
    axes[1].bar(
        positions + width / 2,
        [fold.benchmark_metrics.total_return for fold in result.folds],
        width,
        label="Buy & Hold",
    )
    axes[1].axhline(0, color="#30343b", linewidth=0.8)
    axes[1].set_xticks(positions, labels)
    axes[1].set_ylabel("Fold total return")
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend()
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)

    checks = result.execution_checks
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result.symbol)} 滚动检验报告</title>
  <style>
    body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 1120px;
           margin: 36px auto; padding: 0 20px; color: #172033; line-height: 1.65; }}
    .warning {{ background: #fff4d6; border-left: 5px solid #ef9f27; padding: 12px 16px; }}
    .note {{ background: #eef6ff; border-left: 5px solid #4a83c6; padding: 12px 16px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 14px; }}
    th, td {{ padding: 9px; border-bottom: 1px solid #dfe4ea; text-align: right; }}
    th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    img {{ max-width: 100%; border: 1px solid #e3e8ef; }}
    code {{ background: #f2f4f7; padding: 2px 5px; }}
  </style>
</head>
<body>
  <h1>{html.escape(result.symbol)} 滚动样本外检验</h1>
  <p class="warning"><strong>仅用于学习：</strong>这是历史稳定性诊断，不是新的、尚未观察过的
     样本外证据，也不构成投资建议。</p>
  <p>从 <code>{result.first_oos_date}</code> 开始，每 <code>{result.fold_months}</code> 个月一个
     互不重叠窗口；{html.escape(_strategy_description(result))}和
     {result.cost_bps:.1f} bps 单边成本，不进行参数搜索。完整窗口最少
     {result.min_fold_observations} 个观测；最新的部分窗口会如实纳入。</p>
  <p class="note"><strong>本次观察：</strong>{html.escape(_observation(result))}</p>
  <p>共 {len(result.folds)} 个窗口：{result.positive_folds} 个策略收益为正，
     {result.beats_benchmark_folds} 个跑赢买入持有。聚合策略收益
     <strong>{_percent(result.aggregate_metrics.total_return)}</strong>，聚合基准收益
     <strong>{_percent(result.aggregate_benchmark_metrics.total_return)}</strong>。</p>
  <table>
    <thead><tr><th>窗口</th><th>样本外期间</th><th>策略收益</th><th>基准收益</th>
      <th>策略最大回撤</th><th>夏普</th><th>交易边数</th><th>跑赢基准</th></tr></thead>
    <tbody>{_fold_table(result)}</tbody>
  </table>
  <img src="{html.escape(chart_path.name)}" alt="滚动检验稳定性图">
  <h2>机械执行一致性</h2>
  <p>样本外交易边数 {checks.trade_sides}；缺失或非正开盘价 {checks.missing_execution_prices}；
     信号日期不早于执行日期 {checks.invalid_signal_order}；非前一交易日信号
     {checks.signal_lag_mismatches}；信号值错位 {checks.signal_mismatches}；仓位错位
     {checks.position_mismatches}；换手错位 {checks.turnover_mismatches}；成本错位
     {checks.cost_mismatches}。基础检查结果：
     <strong>{'通过' if checks.passed else '未通过'}</strong>。</p>
  <p>该检查只能验证本地日线数据和信号时点，尚不能证明订单一定成交；涨跌停、停牌、流动性、
     滑点、印花税和冲击成本仍未完整建模。</p>
  <p>实验ID：<code>{html.escape(result.experiment_id)}</code>；数据SHA-256：
     <code>{html.escape(result.data_fingerprint)}</code>。</p>
  <p><a href="{html.escape(csv_path.name)}">打开逐窗口CSV</a></p>
</body>
</html>
"""
    html_path.write_text(document, encoding="utf-8")
    payload = {
        "settings": result.settings_dict(),
        "aggregate_metrics": result.aggregate_metrics.to_dict(),
        "aggregate_benchmark_metrics": result.aggregate_benchmark_metrics.to_dict(),
        "fold_count": len(result.folds),
        "positive_folds": result.positive_folds,
        "beats_benchmark_folds": result.beats_benchmark_folds,
        "execution_checks": result.execution_checks.to_dict(),
        "folds": [fold.to_dict() for fold in result.folds],
        "limitations": [
            "This is a retrospective stability diagnostic, not pristine new OOS evidence.",
            "Parameters are fixed and are not optimized in any fold.",
            "Limit-up, suspension, liquidity, slippage, taxes, and market impact are incomplete.",
            "Historical results are not investment advice.",
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return html_path, chart_path, json_path, csv_path
