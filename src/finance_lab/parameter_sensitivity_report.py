from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle

from finance_lab.config import ProjectPaths
from finance_lab.parameter_sensitivity import ParameterScenario, ParameterSensitivityResult


def _safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _percentage_points(value: float) -> str:
    return f"{value * 100:.2f} 个百分点"


def _cost_label(cost_bps: float) -> str:
    return str(int(cost_bps)) if float(cost_bps).is_integer() else repr(cost_bps)


def _scenario_row(scenario: ParameterScenario) -> dict[str, object]:
    return {
        "short_window": scenario.short_window,
        "long_window": scenario.long_window,
        "strategy_total_return": scenario.metrics.total_return,
        "strategy_annualized_return": scenario.metrics.annualized_return,
        "strategy_max_drawdown": scenario.metrics.max_drawdown,
        "strategy_sharpe": scenario.metrics.sharpe_ratio,
        "benchmark_total_return": scenario.benchmark_metrics.total_return,
        "trade_sides": scenario.metrics.trade_sides,
        "fold_count": scenario.fold_count,
        "positive_folds": scenario.positive_folds,
        "beats_benchmark_folds": scenario.beats_benchmark_folds,
    }


def _scenario_table(result: ParameterSensitivityResult) -> str:
    rows = []
    for scenario in result.scenarios:
        pair = (scenario.short_window, scenario.long_window)
        reference = "（参照）" if pair == result.reference_pair else ""
        rows.append(
            "<tr>"
            f"<td>{scenario.short_window}/{scenario.long_window}{reference}</td>"
            f"<td>{_percent(scenario.metrics.total_return)}</td>"
            f"<td>{_percent(scenario.metrics.annualized_return)}</td>"
            f"<td>{_percent(scenario.metrics.max_drawdown)}</td>"
            f"<td>{scenario.metrics.sharpe_ratio:.2f}</td>"
            f"<td>{scenario.metrics.trade_sides}</td>"
            f"<td>{scenario.positive_folds}/{scenario.fold_count}</td>"
            f"<td>{scenario.beats_benchmark_folds}/{scenario.fold_count}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _observation(result: ParameterSensitivityResult) -> str:
    profitable = result.profitable_scenarios
    total = result.total_scenarios
    if profitable == 0:
        return "全部参数组合的聚合收益均为负，当前规则没有显示出正收益稳健性。"
    if profitable == total:
        return "全部参数组合的聚合收益均为正，但仍需验证其他时期、成本与成交约束。"
    if profitable * 2 < total:
        return f"只有 {profitable}/{total} 组参数为正，结论明显依赖参数选择。"
    return f"{profitable}/{total} 组参数为正，结果仍有分化，不能只展示表现最好的一组。"


def _write_heatmap(result: ParameterSensitivityResult, chart_path: Path) -> None:
    matrix = np.empty((len(result.short_windows), len(result.long_windows)), dtype=float)
    scenario_by_pair = {
        (scenario.short_window, scenario.long_window): scenario
        for scenario in result.scenarios
    }
    for row, short_window in enumerate(result.short_windows):
        for column, long_window in enumerate(result.long_windows):
            matrix[row, column] = scenario_by_pair[
                (short_window, long_window)
            ].metrics.total_return

    max_abs = max(float(np.abs(matrix).max()), 1e-12)
    figure, axis = plt.subplots(figsize=(9.0, 6.0))
    image = axis.imshow(matrix, cmap="RdYlGn", vmin=-max_abs, vmax=max_abs, aspect="auto")
    axis.set_xticks(
        range(len(result.long_windows)),
        labels=[str(value) for value in result.long_windows],
    )
    axis.set_yticks(
        range(len(result.short_windows)),
        labels=[str(value) for value in result.short_windows],
    )
    axis.set_xlabel("Long SMA window")
    axis.set_ylabel("Short SMA window")
    axis.set_title(f"{result.symbol} - walk-forward total return by SMA parameters")
    for row in range(matrix.shape[0]):
        for column in range(matrix.shape[1]):
            value = matrix[row, column]
            text_color = "white" if abs(value) > max_abs * 0.55 else "black"
            axis.text(
                column,
                row,
                f"{value:.1%}",
                ha="center",
                va="center",
                color=text_color,
                fontweight="bold",
            )
    reference_row = result.short_windows.index(result.reference_pair[0])
    reference_column = result.long_windows.index(result.reference_pair[1])
    axis.add_patch(
        Rectangle(
            (reference_column - 0.5, reference_row - 0.5),
            1,
            1,
            fill=False,
            edgecolor="#2166ac",
            linewidth=3,
        )
    )
    colorbar = figure.colorbar(image, ax=axis)
    colorbar.set_label("Walk-forward aggregate total return")
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)


def write_parameter_sensitivity_report(
    result: ParameterSensitivityResult,
    paths: ProjectPaths,
) -> tuple[Path, Path, Path, Path]:
    stem = f"{_safe_symbol(result.symbol)}_{result.strategy}_param_{result.experiment_id}"
    html_path = paths.outputs / f"{stem}_report.html"
    chart_path = paths.outputs / f"{stem}_heatmap.png"
    json_path = paths.outputs / f"{stem}_metrics.json"
    csv_path = paths.outputs / f"{stem}_scenarios.csv"

    scenario_rows = [_scenario_row(scenario) for scenario in result.scenarios]
    pd.DataFrame(scenario_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
    _write_heatmap(result, chart_path)

    reference = result.reference_scenario
    benchmark_return = reference.benchmark_metrics.total_return
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result.symbol)} 参数稳健性检验</title>
  <style>
    body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 1080px;
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
  <h1>{html.escape(result.symbol)} 参数稳健性检验</h1>
  <p class="warning"><strong>这不是参数优化：</strong>不会根据历史结果挑选“最佳参数”，
     只检查相邻参数是否得到相近结论。历史结果不构成投资建议。</p>
  <p>固定数据、{result.fold_months}个月滚动窗口和单边 {_cost_label(result.cost_bps)} bps
     假设成本，只改变短均线 {html.escape(str(result.short_windows))} 与长均线
     {html.escape(str(result.long_windows))}。参照参数为
     <strong>{result.reference_pair[0]}/{result.reference_pair[1]}</strong>。</p>
  <p class="note"><strong>本次观察：</strong>{html.escape(_observation(result))}</p>
  <p>正收益参数：<strong>{result.profitable_scenarios}/{result.total_scenarios}</strong>；
     跑赢相同买入持有基准的参数：
     <strong>{result.beats_benchmark_scenarios}/{result.total_scenarios}</strong>；
     参数收益中位数：<strong>{_percent(result.median_total_return)}</strong>；
     最高与最低参数收益相差：<strong>{_percentage_points(result.return_range)}</strong>。</p>
  <p>参照组 {reference.short_window}/{reference.long_window} 聚合收益
     <strong>{_percent(reference.metrics.total_return)}</strong>；同区间买入持有基准
     <strong>{_percent(benchmark_return)}</strong>。</p>
  <table>
    <thead><tr><th>短/长均线</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th>
      <th>夏普</th><th>交易边数</th><th>正收益窗口</th><th>跑赢基准窗口</th></tr></thead>
    <tbody>{_scenario_table(result)}</tbody>
  </table>
  <img src="{html.escape(chart_path.name)}" alt="参数收益热力图">
  <h2>怎样读这张图</h2>
  <p>蓝框是预先指定的 {result.reference_pair[0]}/{result.reference_pair[1]} 参照组。
     若只有个别格子表现好、相邻格子迅速变差，通常说明
     结果对参数敏感；若大部分相邻格子的方向接近，才有继续研究的价值，但仍不是未来盈利证明。</p>
  <p>实验ID：<code>{html.escape(result.experiment_id)}</code>；数据SHA-256：
     <code>{html.escape(result.data_fingerprint)}</code>。</p>
  <p><a href="{html.escape(csv_path.name)}">打开全部参数CSV</a></p>
</body>
</html>
"""
    html_path.write_text(document, encoding="utf-8")
    payload = {
        "settings": result.settings_dict(),
        "total_scenarios": result.total_scenarios,
        "profitable_scenarios": result.profitable_scenarios,
        "beats_benchmark_scenarios": result.beats_benchmark_scenarios,
        "median_total_return": result.median_total_return,
        "return_range": result.return_range,
        "scenarios": [scenario.to_dict() for scenario in result.scenarios],
        "limitations": [
            "This is a diagnostic grid, not an optimization or model-selection procedure.",
            "All parameter pairs use identical data, walk-forward windows, benchmark, and cost.",
            "Limit-up, suspension, liquidity, slippage, taxes, and market impact are incomplete.",
            "Historical results are not investment advice.",
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return html_path, chart_path, json_path, csv_path
