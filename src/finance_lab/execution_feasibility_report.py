from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd

from finance_lab.config import ProjectPaths
from finance_lab.execution_feasibility import (
    ExecutionFeasibilityResult,
    ExecutionScenario,
)


def _safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _percentage_points(value: float) -> str:
    return f"{value * 100:.2f} 个百分点"


def _scenario_label(scenario: ExecutionScenario) -> str:
    labels = {
        "ideal_next_open": "理想次日开盘",
        "ex_post_daily_bar_proxy": "事后日线阻塞代理",
    }
    if scenario.name.startswith("forced_delay_"):
        return f"额外延迟 {scenario.additional_delay_days} 日"
    return labels.get(scenario.name, scenario.name)


def _scenario_table(result: ExecutionFeasibilityResult) -> str:
    rows: list[str] = []
    for scenario in result.scenarios:
        rows.append(
            "<tr>"
            f"<td>{html.escape(_scenario_label(scenario))}</td>"
            f"<td>{_percent(scenario.metrics.total_return)}</td>"
            f"<td>{_percentage_points(result.return_change_vs_ideal(scenario))}</td>"
            f"<td>{_percent(scenario.metrics.max_drawdown)}</td>"
            f"<td>{scenario.metrics.sharpe_ratio:.2f}</td>"
            f"<td>{scenario.filled_orders}/{scenario.trade_attempts}</td>"
            f"<td>{scenario.blocked_attempts}</td>"
            f"<td>{result.position_difference_days(scenario)}</td>"
            f"<td>{'通过' if scenario.execution_checks.passed else '失败'}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _observation(result: ExecutionFeasibilityResult) -> str:
    delay_change = result.return_change_vs_ideal(result.forced_delay)
    proxy_change = result.return_change_vs_ideal(result.proxy)
    pieces = [
        f"统一额外延迟 {result.forced_delay_days} 个交易日后，累计收益相对理想场景变化"
        f" {_percentage_points(delay_change)}。"
    ]
    if result.proxy.blocked_attempts:
        pieces.append(
            f"事后日线代理记录到 {result.proxy.blocked_attempts} 次受阻尝试，累计收益相对理想场景"
            f"变化 {_percentage_points(proxy_change)}。"
        )
    else:
        pieces.append(
            "本段数据没有出现恰好与策略下单方向重合的事后代理阻塞；这只是未命中代理事件，"
            "不能证明真实市场一定可以成交。"
        )
    return "".join(pieces)


def _write_chart(result: ExecutionFeasibilityResult, chart_path: Path) -> None:
    labels = ["Ideal next open", f"Delay +{result.forced_delay_days}d", "Daily-bar proxy"]
    labels.append("Buy & hold")
    returns = [scenario.metrics.total_return for scenario in result.scenarios] + [
        result.benchmark_metrics.total_return
    ]
    colors = ["#4c78a8", "#f58518", "#54a24b", "#8c8c8c"]
    figure, axis = plt.subplots(figsize=(9.6, 5.8))
    bars = axis.bar(range(len(labels)), returns, color=colors)
    axis.axhline(0, color="#30343b", linewidth=0.8)
    axis.set_xticks(range(len(labels)), labels=labels)
    axis.set_ylabel("Out-of-sample total return")
    axis.set_title(f"{result.symbol} - execution feasibility stress")
    axis.grid(axis="y", alpha=0.25)
    axis.margins(y=0.15)
    for bar, value in zip(bars, returns, strict=True):
        offset = 3 if value >= 0 else -13
        axis.annotate(
            f"{value:.1%}",
            (bar.get_x() + bar.get_width() / 2, value),
            xytext=(0, offset),
            textcoords="offset points",
            ha="center",
            va="bottom" if value >= 0 else "top",
            fontsize=9,
        )
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)


def write_execution_feasibility_report(
    result: ExecutionFeasibilityResult,
    paths: ProjectPaths,
) -> tuple[Path, Path, Path, Path]:
    stem = f"{_safe_symbol(result.symbol)}_sma_exec_{result.experiment_id}"
    html_path = paths.outputs / f"{stem}_report.html"
    chart_path = paths.outputs / f"{stem}_chart.png"
    json_path = paths.outputs / f"{stem}_metrics.json"
    csv_path = paths.outputs / f"{stem}_events.csv"

    events = pd.concat(
        [scenario.events for scenario in result.scenarios],
        ignore_index=True,
    )
    events.to_csv(csv_path, index=False, encoding="utf-8-sig")
    _write_chart(result, chart_path)

    instrument_warning = (
        "<p class=\"warning\"><strong>指数口径限制：</strong>指数本身不能直接交易，"
        "指数日线只能用于教学诊断；真实执行应改用对应ETF、期货或其他可交易工具，并重新"
        "核对价格、费用、跟踪误差和成交规则。</p>"
        if result.instrument_kind == "index"
        else ""
    )
    threshold = _percent(result.lock_threshold_pct)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result.symbol)} 执行可行性压力测试</title>
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
  <h1>{html.escape(result.symbol)} 执行可行性压力测试</h1>
  <p class="warning"><strong>事后代理而非规则：</strong>零成交量、全天单一价格且相对前收跳空至少
     {threshold}，只是保守的日线数据代理，不是当前涨跌停规则，也不能识别盘口排队、瞬时流动性、
     交易所细则、复权误差或真实成交概率。最高价、最低价和全天成交量是在收盘后完整获得，
     所以这是事后分类，不能作为开盘时可知的交易条件。</p>
  {instrument_warning}
  <p>固定使用 {result.short_window}/{result.long_window} 双均线、单边假设成本
     {result.cost_bps!r} bps，从 <code>{result.first_oos_date}</code> 开始比较三种场景：
     信号后下一交易日开盘理想成交、全部目标仓位再延迟 {result.forced_delay_days} 个交易日、
     以及事后日线阻塞代理。受阻时维持原仓位；只要目标仍未改变，就在下一交易日继续尝试；
     只有模拟判定成交时才计算换手和成本。切分日前三个场景均按理想成交初始化，压力条件只从
     首个样本外交易日起生效，避免历史阻塞暗中改变样本外起始仓位。</p>
  <p class="note"><strong>本次观察：</strong>{html.escape(_observation(result))}</p>
  <table>
    <thead><tr><th>场景</th><th>累计收益</th><th>相对理想变化</th><th>最大回撤</th>
      <th>夏普</th><th>成交/尝试</th><th>受阻尝试</th><th>仓位不同天数</th><th>一致性检查</th></tr></thead>
    <tbody>{_scenario_table(result)}</tbody>
  </table>
  <img src="{html.escape(chart_path.name)}" alt="执行可行性压力测试图">
  <h2>应该怎样理解</h2>
  <p>强制延迟是统一的反事实压力，并不预测哪一天真的会延迟；事后日线代理只在特定极端K线出现时
     阻止相应方向的成交。两者都比“默认每次都能按开盘价立刻成交”多了一层检查，但仍没有订单簿、
     盘口排队、冲击成本和券商回报数据。因此它们只能推翻过于乐观的假设，不能证明策略可实盘。</p>
  <p>样本外代理异常K线数：<strong>{result.proxy_flagged_bars}</strong>；实验ID：
     <code>{html.escape(result.experiment_id)}</code>；数据SHA-256：
     <code>{html.escape(result.data_fingerprint)}</code>。</p>
  <p><a href="{html.escape(csv_path.name)}">打开逐次成交尝试CSV</a></p>
</body>
</html>
"""
    html_path.write_text(document, encoding="utf-8")
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
        "limitations": [
            "Daily-bar flags are conservative proxies, not current statutory price-limit rules.",
            "Completed same-day OHLCV is used ex post and is not causal at-open information.",
            "A forced delay is a uniform stress scenario, not a fill-probability model.",
            "All scenarios share ideal pre-OOS state; execution stress starts at the OOS boundary.",
            "Daily bars cannot represent order queues, intraday liquidity, slippage, "
            "or market impact.",
            "An index is not directly tradable; execution results require a tradable instrument.",
            "Historical results are not investment advice.",
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return html_path, chart_path, json_path, csv_path
