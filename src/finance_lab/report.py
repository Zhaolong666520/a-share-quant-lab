from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from finance_lab.backtest import BacktestMetrics, BacktestResult
from finance_lab.config import ProjectPaths


def _safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("/", "_")


def _percent(value: float) -> str:
    return f"{value:.2%}"


def _metric_rows(strategy: BacktestMetrics, benchmark: BacktestMetrics) -> str:
    rows = [
        ("累计收益", _percent(strategy.total_return), _percent(benchmark.total_return)),
        ("年化收益", _percent(strategy.annualized_return), _percent(benchmark.annualized_return)),
        (
            "年化波动",
            _percent(strategy.annualized_volatility),
            _percent(benchmark.annualized_volatility),
        ),
        ("夏普比率", f"{strategy.sharpe_ratio:.2f}", f"{benchmark.sharpe_ratio:.2f}"),
        ("最大回撤", _percent(strategy.max_drawdown), _percent(benchmark.max_drawdown)),
        ("交易边数", str(strategy.trade_sides), str(benchmark.trade_sides)),
        ("持仓比例", _percent(strategy.exposure), _percent(benchmark.exposure)),
    ]
    return "\n".join(
        f"<tr><td>{name}</td><td>{strategy_value}</td><td>{benchmark_value}</td></tr>"
        for name, strategy_value, benchmark_value in rows
    )


def _strategy_description(result: BacktestResult) -> str:
    if result.strategy == "sma":
        return f"{result.short_window}/{result.long_window} 日双均线"
    if result.strategy == "momentum":
        return f"{result.momentum_lookback} 日时间序列动量"
    return "买入并持有"


def write_backtest_report(
    result: BacktestResult,
    paths: ProjectPaths,
    title: str | None = None,
) -> tuple[Path, Path, Path]:
    stem = f"{_safe_symbol(result.symbol)}_{result.strategy}"
    chart_path = paths.outputs / f"{stem}_equity.png"
    html_path = paths.outputs / f"{stem}_report.html"
    json_path = paths.outputs / f"{stem}_metrics.json"

    figure, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(result.frame["trade_date"], result.frame["equity"], label="Strategy", lw=1.8)
    axis.plot(
        result.frame["trade_date"],
        result.frame["benchmark_equity"],
        label="Buy & Hold",
        lw=1.4,
        alpha=0.85,
    )
    axis.set_title(f"{result.symbol} - {result.strategy.upper()} (educational backtest)")
    axis.set_ylabel("Equity (start = 1.0)")
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(chart_path, dpi=150)
    plt.close(figure)

    sources = sorted(str(value) for value in result.frame["source"].dropna().unique())
    source = ", ".join(sources)
    synthetic = any(value.startswith("SYNTHETIC") for value in sources)
    warning = (
        "这是合成演示数据，不是真实行情，结果不能用于投资决策。"
        if synthetic
        else "这是历史研究结果，不代表未来收益，也不是投资建议。"
    )
    display_title = title or f"{result.symbol} 回测学习报告"
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(display_title)}</title>
  <style>
    body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 960px;
           margin: 36px auto; padding: 0 20px; color: #172033; line-height: 1.65; }}
    .warning {{ background: #fff4d6; border-left: 5px solid #ef9f27; padding: 12px 16px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #dfe4ea; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    img {{ max-width: 100%; border: 1px solid #e3e8ef; }}
    code {{ background: #f2f4f7; padding: 2px 5px; }}
  </style>
</head>
<body>
  <h1>{html.escape(display_title)}</h1>
  <p class="warning"><strong>重要：</strong>{html.escape(warning)}</p>
  <p>数据来源：<code>{html.escape(source)}</code>；策略：
     <code>{html.escape(_strategy_description(result))}</code>；
     单边成本假设：<code>{result.cost_bps:.1f} bps</code>。</p>
  <p>信号在当日收盘后生成，最早于下一交易日开盘执行，避免使用当日尚未知道的信息。</p>
  <table>
    <thead><tr><th>指标</th><th>策略</th><th>买入持有基准</th></tr></thead>
    <tbody>{_metric_rows(result.metrics, result.benchmark_metrics)}</tbody>
  </table>
  <img src="{html.escape(chart_path.name)}" alt="回测净值曲线">
  <h2>如何理解</h2>
  <p>先看最大回撤，再看收益；交易次数越多，结果对成本和滑点越敏感。只有样本外结果、
     数据质量检查和现实交易约束同时通过，策略才值得进一步研究。</p>
</body>
</html>
"""
    html_path.write_text(document, encoding="utf-8")
    payload = {
        "symbol": result.symbol,
        "strategy": result.strategy,
        "strategy_description": _strategy_description(result),
        "short_window": result.short_window,
        "long_window": result.long_window,
        "momentum_lookback": result.momentum_lookback,
        "source": source,
        "synthetic": synthetic,
        "cost_bps": result.cost_bps,
        "metrics": result.metrics.to_dict(),
        "benchmark_metrics": result.benchmark_metrics.to_dict(),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return html_path, chart_path, json_path
