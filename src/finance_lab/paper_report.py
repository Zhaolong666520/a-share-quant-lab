"""Deterministic local audit exports for forward paper-trading accounts."""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd

from finance_lab.config import ProjectPaths
from finance_lab.paper_models import PaperDataContext, PaperOperationResult, validate_portfolio_id
from finance_lab.paper_store import PaperPortfolioSnapshot, PaperStore


@dataclass(frozen=True)
class PaperReportPaths:
    archive_html: Path
    archive_json: Path
    daily_csv: Path
    trades_csv: Path
    distributions_csv: Path
    chart_png: Path
    latest_html: Path
    latest_json: Path

    @property
    def all_paths(self) -> tuple[Path, ...]:
        return (
            self.archive_html,
            self.archive_json,
            self.daily_csv,
            self.trades_csv,
            self.distributions_csv,
            self.chart_png,
            self.latest_html,
            self.latest_json,
        )


def write_paper_report(
    result: PaperOperationResult,
    paths: ProjectPaths,
) -> PaperReportPaths:
    """Write immutable report archives and refresh safe latest-report pointers."""
    portfolio_id = validate_portfolio_id(result.portfolio_id)
    with PaperStore(paths.database, read_only=True) as store:
        snapshot = store.portfolio_snapshot(portfolio_id)
    daily = _query_frame(
        paths.database,
        """
        SELECT event.account_id, event.trade_date, event.cash_after AS cash,
               event.shares_after AS shares, event.close_price, event.equity_after AS equity,
               event.drawdown_after AS drawdown
        FROM paper_events AS event
        INNER JOIN paper_accounts AS account USING (account_id)
        WHERE account.portfolio_id = ? AND event.event_type = 'VALUATION'
        ORDER BY event.trade_date, account.account_id
        """,
        [portfolio_id],
    )
    trades = _query_frame(
        paths.database,
        """
        SELECT event.account_id, event.trade_date, event.signal_date, event.order_id,
               event.action, event.quantity, event.reference_price, event.execution_price,
               event.notional, event.commission, event.tax, event.slippage_cost,
               event.cash_after, event.shares_after, event.reason_code
        FROM paper_events AS event
        INNER JOIN paper_accounts AS account USING (account_id)
        WHERE account.portfolio_id = ? AND event.event_type = 'ORDER_FILLED'
        ORDER BY event.trade_date, account.account_id, event.sequence_no
        """,
        [portfolio_id],
    )
    distributions = _query_frame(
        paths.database,
        """
        SELECT
            event.account_id, event.trade_date, event.event_type,
            json_extract_string(event.payload_json, '$.action_id') AS action_id,
            json_extract_string(event.payload_json, '$.record_date') AS record_date,
            json_extract_string(event.payload_json, '$.payment_date') AS payment_date,
            CAST(json_extract(event.payload_json, '$.cash_per_share') AS DOUBLE)
                AS cash_per_share,
            event.quantity, event.notional AS cash_amount, event.reason_code,
            json_extract_string(event.payload_json, '$.source_url') AS source_url
        FROM paper_events AS event
        INNER JOIN paper_accounts AS account USING (account_id)
        WHERE account.portfolio_id = ?
          AND event.event_type LIKE 'CASH_DISTRIBUTION_%'
        ORDER BY event.trade_date, event.account_id, event.sequence_no
        """,
        [portfolio_id],
    )
    payload = _report_payload(result, snapshot, daily, trades, distributions)
    stem = _archive_stem(portfolio_id, snapshot)
    archive_html = _output_path(paths, f"{stem}_report.html")
    archive_json = _output_path(paths, f"{stem}_report.json")
    daily_csv = _output_path(paths, f"{stem}_daily.csv")
    trades_csv = _output_path(paths, f"{stem}_trades.csv")
    distributions_csv = _output_path(paths, f"{stem}_distributions.csv")
    chart_png = _output_path(paths, f"{stem}_equity.png")
    latest_html = _output_path(paths, f"{portfolio_id}_paper_latest_report.html")
    latest_json = _output_path(paths, f"{portfolio_id}_paper_latest_report.json")
    outputs = PaperReportPaths(
        archive_html=archive_html,
        archive_json=archive_json,
        daily_csv=daily_csv,
        trades_csv=trades_csv,
        distributions_csv=distributions_csv,
        chart_png=chart_png,
        latest_html=latest_html,
        latest_json=latest_json,
    )

    document = _html_document(
        payload,
        chart_png.name,
        daily_csv.name,
        trades_csv.name,
        distributions_csv.name,
    )
    if not archive_json.exists():
        _write_json(archive_json, payload)
    if not daily_csv.exists():
        daily.to_csv(daily_csv, index=False, encoding="utf-8-sig")
    if not trades_csv.exists():
        trades.to_csv(trades_csv, index=False, encoding="utf-8-sig")
    if not distributions_csv.exists():
        distributions.to_csv(distributions_csv, index=False, encoding="utf-8-sig")
    if not chart_png.exists():
        _write_chart(daily, snapshot, chart_png)
    if not archive_html.exists():
        archive_html.write_text(document, encoding="utf-8")
    latest_html.write_text(document, encoding="utf-8")
    _write_json(latest_json, payload)
    return outputs


def _query_frame(database: Path, query: str, parameters: list[object]) -> pd.DataFrame:
    with duckdb.connect(str(database), read_only=True) as connection:
        cursor = connection.execute(query, parameters)
        columns = [str(item[0]) for item in cursor.description]
        return pd.DataFrame(cursor.fetchall(), columns=columns)


def _archive_stem(portfolio_id: str, snapshot: PaperPortfolioSnapshot) -> str:
    accounts = snapshot["accounts"]
    if not accounts:
        raise ValueError("模拟账户组没有可报告的账户")
    dates = {str(account["state"]["last_trade_date"]) for account in accounts}
    if len(dates) != 1:
        raise ValueError("模拟账户最后处理日期不一致，不能生成报告")
    latest_date = next(iter(dates))
    identity = {
        "portfolio_id": portfolio_id,
        "last_trade_date": latest_date,
        "accounts": [
            {
                "account_id": account["account_id"],
                "dataset_id": account["latest_run"]["dataset_id"],
                "curated_sha256": account["latest_run"]["curated_sha256"],
                "config_hash": account["config_hash"],
                "state_hash": account["state"]["last_event_hash"],
            }
            for account in accounts
        ],
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    context_id = hashlib.sha256(canonical.encode("ascii")).hexdigest()[:12]
    return f"{portfolio_id}_paper_{latest_date}_r{context_id}"


def _report_payload(
    result: PaperOperationResult,
    snapshot: PaperPortfolioSnapshot,
    daily: pd.DataFrame,
    trades: pd.DataFrame,
    distributions: pd.DataFrame,
) -> dict[str, object]:
    accounts: list[dict[str, object]] = []
    for account in snapshot["accounts"]:
        state = account["state"]
        ledger_config = account["ledger_config"]
        initial_cash = _number(ledger_config["initial_cash"], "初始资金")
        equity = _number(state["equity"], "账户权益")
        account_id = account["account_id"]
        account_trades = trades.loc[trades["account_id"] == account_id]
        account_distributions = distributions.loc[
            distributions["account_id"] == account_id
        ]
        paid_distributions = account_distributions.loc[
            account_distributions["event_type"] == "CASH_DISTRIBUTION_PAID"
        ]
        pending_entitlements = state["distribution_entitlements"]
        assert isinstance(pending_entitlements, list)
        accounts.append(
            {
                **account,
                "metrics": {
                    "market_value": _number(state["shares"], "账户份额")
                    * _number(state["last_close"], "账户收盘价"),
                    "cumulative_return": equity / initial_cash - 1.0,
                    "trade_sides": int(len(account_trades)),
                    "commission_total": float(account_trades["commission"].sum()),
                    "tax_total": float(account_trades["tax"].sum()),
                    "slippage_total": float(account_trades["slippage_cost"].sum()),
                    "distribution_cash_total": float(
                        paid_distributions["cash_amount"].sum()
                    ),
                    "pending_distribution_cash": sum(
                        _number(item["cash_amount"], "待到账分红")
                        for item in pending_entitlements
                        if isinstance(item, dict)
                    ),
                    "pending_distribution_count": len(pending_entitlements),
                },
            }
        )
    return {
        "portfolio_id": result.portfolio_id,
        "operation_status": result.status,
        "processed_dates": [item.isoformat() for item in result.processed_dates],
        "data_context": _data_context_payload(result.data_context),
        "accounts": accounts,
        "daily_rows": int(len(daily)),
        "trade_rows": int(len(trades)),
        "distribution_rows": int(len(distributions)),
        "disclaimer": "模拟盘、非实盘、非投资建议；费用与滑点均为假设情景。",
        "limitations": [
            "不连接券商，不发送真实订单。",
            "固定滑点开盘成交是模拟假设，不代表实际成交结果。",
            "现金分红只接受本地可审计快照；缺失、格式错误或迟到快照不会被估算。",
            "暂未建模拆分合并、停牌和部分成交。",
        ],
    }


def _data_context_payload(context: PaperDataContext | None) -> dict[str, object] | None:
    if context is None:
        return None
    return {
        **asdict(context),
        "as_of_date": context.as_of_date.isoformat(),
        "data_start_date": context.data_start_date.isoformat(),
        "data_end_date": context.data_end_date.isoformat(),
        "warning_codes": list(context.warning_codes),
    }


def _write_chart(
    daily: pd.DataFrame,
    snapshot: PaperPortfolioSnapshot,
    output: Path,
) -> None:
    figure, (equity_axis, drawdown_axis) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    for account in snapshot["accounts"]:
        account_id = account["account_id"]
        frame = daily.loc[daily["account_id"] == account_id]
        initial_cash = _number(account["ledger_config"]["initial_cash"], "初始资金")
        label = f"{account_id} ({account['strategy']})"
        equity_axis.plot(frame["trade_date"], frame["equity"] / initial_cash, label=label)
        drawdown_axis.plot(frame["trade_date"], frame["drawdown"], label=label)
    equity_axis.set_title("Forward paper-trading account equity")
    equity_axis.set_ylabel("Equity (initial = 1.0)")
    drawdown_axis.set_ylabel("Drawdown")
    drawdown_axis.set_xlabel("Trade date")
    equity_axis.grid(alpha=0.25)
    drawdown_axis.grid(alpha=0.25)
    equity_axis.legend()
    drawdown_axis.legend()
    figure.tight_layout()
    figure.savefig(output, dpi=150)
    plt.close(figure)


def _html_document(
    payload: dict[str, object],
    chart_name: str,
    daily_csv_name: str,
    trades_csv_name: str,
    distributions_csv_name: str,
) -> str:
    raw_accounts = payload["accounts"]
    assert isinstance(raw_accounts, list)
    account_rows: list[str] = []
    lineage_blocks: list[str] = []
    for raw_account in raw_accounts:
        assert isinstance(raw_account, dict)
        state = raw_account["state"]
        metrics = raw_account["metrics"]
        latest_run = raw_account["latest_run"]
        assert isinstance(state, dict)
        assert isinstance(metrics, dict)
        assert isinstance(latest_run, dict)
        account_rows.append(
            "<tr>"
            f"<td>{html.escape(str(raw_account['account_id']))}</td>"
            f"<td>{html.escape(str(raw_account['strategy']))}</td>"
            f"<td>{float(state['cash']):,.2f}</td>"
            f"<td>{int(state['shares']):,}</td>"
            f"<td>{float(metrics['market_value']):,.2f}</td>"
            f"<td>{float(state['equity']):,.2f}</td>"
            f"<td>{float(metrics['cumulative_return']):.2%}</td>"
            f"<td>{float(state['drawdown']):.2%}</td>"
            f"<td>{int(metrics['trade_sides'])}</td>"
            f"<td>{float(metrics['pending_distribution_cash']):,.2f}</td>"
            f"<td>{float(metrics['distribution_cash_total']):,.2f}</td>"
            f"<td>{html.escape(str(state['pending_order'] or '无'))}</td>"
            "</tr>"
        )
        lineage_blocks.append(
            "<li>"
            f"<code>{html.escape(str(raw_account['account_id']))}</code>：数据集 "
            f"<code>{html.escape(str(latest_run['dataset_id']))}</code>；文件 SHA-256 "
            f"<code>{html.escape(str(latest_run['curated_sha256']))}</code>；"
            f"数据提醒 <code>{html.escape(str(latest_run['data_health']))}</code>。"
            "</li>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(str(payload['portfolio_id']))} 模拟盘审计报告</title><style>
body {{ font-family: system-ui, "Microsoft YaHei", sans-serif; max-width: 1180px; margin: 36px auto;
padding: 0 20px; color: #172033; line-height: 1.65; }}
.warning {{ background: #fff4d6; border-left: 5px solid #ef9f27; padding: 12px 16px; }}
.note {{ background: #eef6ff; border-left: 5px solid #4a83c6; padding: 12px 16px; }}
table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }}
th,td {{ padding: 9px; border-bottom: 1px solid #dfe4ea; text-align: right; }}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2) {{ text-align: left; }}
code {{ background: #f2f4f7; padding: 2px 5px; overflow-wrap: anywhere; }}
img {{ max-width: 100%; }}
</style></head><body>
<h1>{html.escape(str(payload['portfolio_id']))} 前向模拟盘审计报告</h1>
<p class="warning"><strong>重要：</strong>{html.escape(str(payload['disclaimer']))}</p>
<p class="note">本报告只读取已经提交的本地事件账本。信号在收盘后生成，订单最早在下一根已验证日线
开盘按假设价格模拟成交。</p>
<table><thead><tr><th>账户</th><th>固定策略</th><th>现金</th><th>份额</th><th>持仓市值</th>
<th>权益</th><th>累计收益</th><th>当前回撤</th><th>成交边数</th><th>待到账分红</th>
<th>累计分红到账</th><th>待成交订单</th></tr></thead>
<tbody>{''.join(account_rows)}</tbody></table>
<img src="{html.escape(chart_name)}" alt="两个模拟账户的净值和回撤图">
<p><a href="{html.escape(daily_csv_name)}">逐日权益 CSV</a>；
<a href="{html.escape(trades_csv_name)}">成交明细 CSV</a>；
<a href="{html.escape(distributions_csv_name)}">分红明细 CSV</a></p>
<h2>数据血缘</h2><ul>{''.join(lineage_blocks)}</ul>
</body></html>"""


def _output_path(paths: ProjectPaths, filename: str) -> Path:
    root = paths.outputs.resolve()
    candidate = (root / filename).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("报告输出路径越过 outputs 目录")
    return candidate


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"报告中的{label}不是有限数字")
    return float(value)
