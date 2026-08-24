from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd

from finance_lab.config import ProjectPaths
from finance_lab.cost_sensitivity import CostScenario, CostSensitivityResult


def _safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _percentage_points(value: float) -> str:
    return f"{value * 100:.2f} 个百分点"


def _cost_label(cost_bps: float) -> str:
    return str(int(cost_bps)) if cost_bps.is_integer() else repr(cost_bps)


def _scenario_row(scenario: CostScenario) -> dict[str, object]:
    return {
        "cost_bps": scenario.cost_bps,
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


def _scenario_table(result: CostSensitivityResult) -> str:
    rows = []
    for scenario in result.scenarios:
        rows.append(
            "<tr>"
            f"<td>{_cost_label(scenario.cost_bps)} bps</td>"
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


def _observation(result: CostSensitivityResult) -> str:
    lowest = result.scenarios[0]
    highest = result.scenarios[-1]
    if lowest.metrics.total_return < 0:
        return "即使采用最低假设成本，聚合收益仍为负；提高成本只会进一步恶化结果。"
    if highest.metrics.total_return < 0:
        return "策略在低成本情景盈利，但在高成本压力下转为亏损，说明成本敏感。"
    return "所有情景仍为正收益，但这不能替代成交约束和未来数据验证。"


def write_cost_sensitivity_report(
    result: CostSensitivityResult,
    paths: ProjectPaths,
) -> tuple[Path, Path, Path, Path]:
    stem = f"{_safe_symbol(result.symbol)}_{result.strategy}_cost_{result.experiment_id}"
    html_path = paths.outputs / f"{stem}_report.html"
    chart_path = paths.outputs / f"{stem}_chart.png"
    json_path = paths.outputs / f"{stem}_metrics.json"
    csv_path = paths.outputs / f"{stem}_scenarios.csv"

    scenario_rows = [_scenario_row(scenario) for scenario in result.scenarios]
    pd.DataFrame(scenario_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    costs = [scenario.cost_bps for scenario in result.scenarios]
    strategy_returns = [scenario.metrics.total_return for scenario in result.scenarios]
    benchmark_returns = [scenario.benchmark_metrics.total_return for scenario in result.scenarios]
    figure, axis = plt.subplots(figsize=(9.5, 5.6))
    axis.plot(costs, strategy_returns, marker="o", lw=2.0, label="Strategy")
    axis.plot(costs, benchmark_returns, marker="o", lw=1.4, label="Buy & Hold")
    axis.axhline(0, color="#30343b", linewidth=0.8)
    axis.set_title(f"{result.symbol} - hypothetical one-way cost stress")
    axis.set_xlabel("Cost per trade side (bps)")
    axis.set_ylabel("Walk-forward aggregate total return")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)

    lowest = result.scenarios[0]
    highest = result.scenarios[-1]
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result.symbol)} 交易成本压力测试</title>
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
  <h1>{html.escape(result.symbol)} 交易成本压力测试</h1>
  <p class="warning"><strong>假设情景：</strong>以下数字不代表当前券商费率、税率或实际成交成本，
     仅用于测试策略对成本的敏感程度，不构成投资建议。</p>
  <p>固定使用 {result.short_window}/{result.long_window} 双均线，从
     <code>{result.first_oos_date}</code> 开始进行{result.fold_months}个月滚动诊断。只改变单边
     成本，信号、仓位、数据和窗口均保持不变。</p>
  <p class="note"><strong>本次观察：</strong>{html.escape(_observation(result))}</p>
  <p>成本从 {_cost_label(lowest.cost_bps)} bps 提高到
     {_cost_label(highest.cost_bps)} bps 后，聚合收益变化
     <strong>{_percentage_points(result.high_cost_return_change)}</strong>；收益随成本单调不增加：
     <strong>{'是' if result.monotonic_non_increasing else '否'}</strong>。</p>
  <table>
    <thead><tr><th>单边成本</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th>
      <th>夏普</th><th>交易边数</th><th>正收益窗口</th><th>跑赢基准窗口</th></tr></thead>
    <tbody>{_scenario_table(result)}</tbody>
  </table>
  <img src="{html.escape(chart_path.name)}" alt="成本压力测试图">
  <h2>基准线为什么可能不变</h2>
  <p>滚动区间开始前，买入持有基准已经建立仓位；区间内没有换手，因此不同成本情景可能不会
     改变基准收益。策略有多次买卖，成本会在每个交易边扣除。</p>
  <p>实验ID：<code>{html.escape(result.experiment_id)}</code>；数据SHA-256：
     <code>{html.escape(result.data_fingerprint)}</code>。</p>
  <p><a href="{html.escape(csv_path.name)}">打开成本情景CSV</a></p>
</body>
</html>
"""
    html_path.write_text(document, encoding="utf-8")
    payload = {
        "settings": result.settings_dict(),
        "monotonic_non_increasing": result.monotonic_non_increasing,
        "high_cost_return_change": result.high_cost_return_change,
        "scenarios": [scenario.to_dict() for scenario in result.scenarios],
        "limitations": [
            "Scenario costs are hypothetical and are not current broker or statutory rates.",
            "Signals, positions, data, and windows are fixed across scenarios.",
            "Limit-up, suspension, liquidity, slippage, taxes, and market impact are incomplete.",
            "Historical results are not investment advice.",
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return html_path, chart_path, json_path, csv_path
