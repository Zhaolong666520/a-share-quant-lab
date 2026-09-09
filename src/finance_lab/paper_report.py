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
from finance_lab.paper_attribution import build_daily_attribution
from finance_lab.paper_benchmark import (
    calculate_relative_performance,
    calculate_shared_price_benchmark,
)
from finance_lab.paper_models import PaperDataContext, PaperOperationResult, validate_portfolio_id
from finance_lab.paper_observation import (
    PaperObservationProgress,
    build_forward_observation_history,
    calculate_observation_progress,
)
from finance_lab.paper_risk import (
    DEFAULT_PAPER_RISK_POLICY,
    PaperRiskPolicy,
    calculate_paper_risk,
)
from finance_lab.paper_store import PaperPortfolioSnapshot, PaperStore
from finance_lab.validation import assert_valid_daily_prices

PAPER_REPORT_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class PaperReportPaths:
    archive_html: Path
    archive_json: Path
    daily_csv: Path
    observation_history_csv: Path
    trades_csv: Path
    order_events_csv: Path
    attribution_csv: Path
    distributions_csv: Path
    share_adjustments_csv: Path
    chart_png: Path
    latest_html: Path
    latest_json: Path

    @property
    def all_paths(self) -> tuple[Path, ...]:
        return (
            self.archive_html,
            self.archive_json,
            self.daily_csv,
            self.observation_history_csv,
            self.trades_csv,
            self.order_events_csv,
            self.attribution_csv,
            self.distributions_csv,
            self.share_adjustments_csv,
            self.chart_png,
            self.latest_html,
            self.latest_json,
        )


def write_paper_report(
    result: PaperOperationResult,
    paths: ProjectPaths,
    *,
    risk_policy: PaperRiskPolicy = DEFAULT_PAPER_RISK_POLICY,
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
    order_events = _query_frame(
        paths.database,
        """
        SELECT
            event.account_id, event.trade_date, event.event_type, event.signal_date,
            event.order_id, event.action, event.quantity, event.reference_price,
            event.execution_price, event.reason_code,
            COALESCE(
                CAST(json_extract(event.payload_json, '$.attempt_count') AS INTEGER),
                CASE WHEN event.event_type = 'ORDER_CREATED' THEN 0 ELSE NULL END
            ) AS attempt_count,
            json_extract_string(event.payload_json, '$.last_failure_reason')
                AS last_failure_reason
        FROM paper_events AS event
        INNER JOIN paper_accounts AS account USING (account_id)
        WHERE account.portfolio_id = ? AND event.event_type LIKE 'ORDER_%'
        ORDER BY event.trade_date, event.account_id, event.sequence_no
        """,
        [portfolio_id],
    )
    attribution_events = _query_frame(
        paths.database,
        """
        SELECT
            event.account_id, event.trade_date, event.sequence_no, event.event_type,
            event.action, event.quantity, event.reference_price, event.commission,
            event.tax, event.slippage_cost, event.notional, event.cash_after,
            event.shares_after, event.close_price, event.equity_after, event.payload_json
        FROM paper_events AS event
        INNER JOIN paper_accounts AS account USING (account_id)
        WHERE account.portfolio_id = ?
          AND event.event_type IN (
              'VALUATION', 'ORDER_FILLED', 'SHARE_ADJUSTMENT_APPLIED',
              'CASH_DISTRIBUTION_PAID'
          )
        ORDER BY event.account_id, event.trade_date, event.sequence_no
        """,
        [portfolio_id],
    )
    attribution = build_daily_attribution(
        attribution_events,
        _attribution_prices(paths, snapshot, attribution_events),
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
    share_adjustments = _query_frame(
        paths.database,
        """
        SELECT
            event.account_id, event.trade_date, event.event_type,
            json_extract_string(event.payload_json, '$.action_id') AS action_id,
            json_extract_string(event.payload_json, '$.effective_date') AS effective_date,
            CAST(json_extract(event.payload_json, '$.ratio_numerator') AS BIGINT)
                AS ratio_numerator,
            CAST(json_extract(event.payload_json, '$.ratio_denominator') AS BIGINT)
                AS ratio_denominator,
            CAST(json_extract(event.payload_json, '$.shares_before') AS BIGINT)
                AS shares_before,
            CAST(json_extract(event.payload_json, '$.shares_after') AS BIGINT)
                AS shares_after,
            event.quantity AS share_change, event.reason_code,
            json_extract_string(event.payload_json, '$.source_url') AS source_url,
            json_extract_string(event.payload_json, '$.source_published_at')
                AS source_published_at,
            json_extract_string(event.payload_json, '$.ingested_at') AS ingested_at
        FROM paper_events AS event
        INNER JOIN paper_accounts AS account USING (account_id)
        WHERE account.portfolio_id = ?
          AND event.event_type LIKE 'SHARE_ADJUSTMENT_%'
        ORDER BY event.trade_date, event.account_id, event.sequence_no
        """,
        [portfolio_id],
    )
    initial_cash_by_account = {
        str(account["account_id"]): _number(
            account["ledger_config"]["initial_cash"], "初始资金"
        )
        for account in snapshot["accounts"]
    }
    observation_progress = calculate_observation_progress(
        daily,
        minimum_evidence_observations=risk_policy.minimum_return_observations,
    )
    observation_history = build_forward_observation_history(
        daily,
        initial_cash_by_account=initial_cash_by_account,
        minimum_evidence_observations=risk_policy.minimum_return_observations,
    )
    payload = _report_payload(
        result,
        snapshot,
        daily,
        trades,
        order_events,
        attribution,
        distributions,
        share_adjustments,
        observation_progress,
        observation_history,
        risk_policy,
    )
    stem = _archive_stem(portfolio_id, snapshot, risk_policy)
    archive_html = _output_path(paths, f"{stem}_report.html")
    archive_json = _output_path(paths, f"{stem}_report.json")
    daily_csv = _output_path(paths, f"{stem}_daily.csv")
    observation_history_csv = _output_path(
        paths, f"{stem}_observation_history.csv"
    )
    trades_csv = _output_path(paths, f"{stem}_trades.csv")
    order_events_csv = _output_path(paths, f"{stem}_order_events.csv")
    attribution_csv = _output_path(paths, f"{stem}_attribution.csv")
    distributions_csv = _output_path(paths, f"{stem}_distributions.csv")
    share_adjustments_csv = _output_path(paths, f"{stem}_share_adjustments.csv")
    chart_png = _output_path(paths, f"{stem}_equity.png")
    latest_html = _output_path(paths, f"{portfolio_id}_paper_latest_report.html")
    latest_json = _output_path(paths, f"{portfolio_id}_paper_latest_report.json")
    outputs = PaperReportPaths(
        archive_html=archive_html,
        archive_json=archive_json,
        daily_csv=daily_csv,
        observation_history_csv=observation_history_csv,
        trades_csv=trades_csv,
        order_events_csv=order_events_csv,
        attribution_csv=attribution_csv,
        distributions_csv=distributions_csv,
        share_adjustments_csv=share_adjustments_csv,
        chart_png=chart_png,
        latest_html=latest_html,
        latest_json=latest_json,
    )

    document = _html_document(
        payload,
        chart_png.name,
        daily_csv.name,
        observation_history_csv.name,
        trades_csv.name,
        order_events_csv.name,
        attribution_csv.name,
        distributions_csv.name,
        share_adjustments_csv.name,
    )
    if not archive_json.exists():
        _write_json(archive_json, payload)
    if not daily_csv.exists():
        daily.to_csv(daily_csv, index=False, encoding="utf-8-sig")
    if not observation_history_csv.exists():
        observation_history.to_csv(
            observation_history_csv,
            index=False,
            encoding="utf-8-sig",
        )
    if not trades_csv.exists():
        trades.to_csv(trades_csv, index=False, encoding="utf-8-sig")
    if not order_events_csv.exists():
        order_events.to_csv(order_events_csv, index=False, encoding="utf-8-sig")
    if not attribution_csv.exists():
        attribution.to_csv(attribution_csv, index=False, encoding="utf-8-sig")
    if not distributions_csv.exists():
        distributions.to_csv(distributions_csv, index=False, encoding="utf-8-sig")
    if not share_adjustments_csv.exists():
        share_adjustments.to_csv(
            share_adjustments_csv,
            index=False,
            encoding="utf-8-sig",
        )
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


def _attribution_prices(
    paths: ProjectPaths,
    snapshot: PaperPortfolioSnapshot,
    attribution_events: pd.DataFrame,
) -> pd.DataFrame:
    accounts = snapshot["accounts"]
    symbols = {str(account["symbol"]) for account in accounts}
    expected_hashes = {str(account["latest_run"]["curated_sha256"]) for account in accounts}
    if len(symbols) != 1 or len(expected_hashes) != 1:
        raise ValueError("收益归因只支持共享同一数据快照的单标的账户组")
    symbol = next(iter(symbols))
    valuations = attribution_events.loc[
        attribution_events["event_type"] == "VALUATION"
    ]
    rows: list[dict[str, object]] = []
    legacy_valuations: list[pd.Series] = []
    for _, valuation in valuations.iterrows():
        payload = json.loads(str(valuation["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError("估值事件 payload 必须是 JSON 对象")
        open_price = payload.get("open_price")
        if open_price is None:
            legacy_valuations.append(valuation)
            continue
        rows.append(
            {
                "account_id": str(valuation["account_id"]),
                "trade_date": valuation["trade_date"],
                "open_price": open_price,
                "verified_close": valuation["close_price"],
            }
        )
    if not legacy_valuations:
        return pd.DataFrame(rows)

    safe_symbol = symbol.replace(".", "_").replace("/", "_")
    curated_path = (paths.curated / f"{safe_symbol}.parquet").resolve()
    if not curated_path.is_relative_to(paths.curated.resolve()) or not curated_path.is_file():
        raise ValueError("旧估值事件缺少开盘价，且找不到兼容回填所需的整理行情")
    prices = pd.read_parquet(curated_path)
    assert_valid_daily_prices(prices)
    prices = prices.loc[prices["symbol"].astype(str) == symbol].copy()
    if prices.empty:
        raise ValueError("收益归因行情中没有模拟盘标的")
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    for valuation in legacy_valuations:
        trade_date = pd.Timestamp(valuation["trade_date"])
        matches = prices.loc[prices["trade_date"] == trade_date]
        if len(matches) != 1:
            raise ValueError("旧估值事件无法恰好匹配一条整理行情")
        price = matches.iloc[0]
        rows.append(
            {
                "account_id": str(valuation["account_id"]),
                "trade_date": valuation["trade_date"],
                "open_price": price["open"],
                "verified_close": price["close"],
            }
        )
    return pd.DataFrame(rows)


def _archive_stem(
    portfolio_id: str,
    snapshot: PaperPortfolioSnapshot,
    risk_policy: PaperRiskPolicy,
) -> str:
    accounts = snapshot["accounts"]
    if not accounts:
        raise ValueError("模拟账户组没有可报告的账户")
    dates = {str(account["state"]["last_trade_date"]) for account in accounts}
    if len(dates) != 1:
        raise ValueError("模拟账户最后处理日期不一致，不能生成报告")
    latest_date = next(iter(dates))
    identity = {
        "report_schema_version": PAPER_REPORT_SCHEMA_VERSION,
        "risk_policy": asdict(risk_policy),
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
    order_events: pd.DataFrame,
    attribution: pd.DataFrame,
    distributions: pd.DataFrame,
    share_adjustments: pd.DataFrame,
    observation_progress: PaperObservationProgress,
    observation_history: pd.DataFrame,
    risk_policy: PaperRiskPolicy,
) -> dict[str, object]:
    forward_benchmark = calculate_shared_price_benchmark(daily)
    accounts: list[dict[str, object]] = []
    for account in snapshot["accounts"]:
        state = account["state"]
        ledger_config = account["ledger_config"]
        initial_cash = _number(ledger_config["initial_cash"], "初始资金")
        equity = _number(state["equity"], "账户权益")
        account_id = account["account_id"]
        account_daily = daily.loc[daily["account_id"] == account_id]
        account_trades = trades.loc[trades["account_id"] == account_id]
        account_order_events = order_events.loc[order_events["account_id"] == account_id]
        account_attribution = attribution.loc[attribution["account_id"] == account_id]
        account_distributions = distributions.loc[
            distributions["account_id"] == account_id
        ]
        paid_distributions = account_distributions.loc[
            account_distributions["event_type"] == "CASH_DISTRIBUTION_PAID"
        ]
        account_adjustments = share_adjustments.loc[
            share_adjustments["account_id"] == account_id
        ]
        applied_adjustments = account_adjustments.loc[
            account_adjustments["event_type"] == "SHARE_ADJUSTMENT_APPLIED"
        ]
        pending_entitlements = state["distribution_entitlements"]
        assert isinstance(pending_entitlements, list)
        pending_order = state["pending_order"]
        if pending_order is not None and not isinstance(pending_order, dict):
            raise ValueError("报告中的待成交订单格式无效")
        pending_attempt_count = (
            int(pending_order.get("attempt_count", 0))
            if isinstance(pending_order, dict)
            else None
        )
        pending_order_age_days = (
            int(
                (
                    pd.Timestamp(str(state["last_trade_date"]))
                    - pd.Timestamp(str(pending_order["signal_date"]))
                ).days
            )
            if isinstance(pending_order, dict)
            else None
        )
        risk_snapshot = calculate_paper_risk(account_daily, risk_policy)
        relative_performance = calculate_relative_performance(
            account_daily,
            initial_cash=initial_cash,
            benchmark=forward_benchmark,
            risk_gate_status=risk_snapshot.gate_status,
        )
        accounts.append(
            {
                **account,
                "risk": {
                    **asdict(risk_snapshot),
                    "breach_codes": list(risk_snapshot.breach_codes),
                },
                "relative_performance": asdict(relative_performance),
                "metrics": {
                    "market_value": _number(state["shares"], "账户份额")
                    * _number(state["last_close"], "账户收盘价"),
                    "cumulative_return": equity / initial_cash - 1.0,
                    "trade_sides": int(len(account_trades)),
                    "order_created_count": int(
                        (account_order_events["event_type"] == "ORDER_CREATED").sum()
                    ),
                    "order_deferred_count": int(
                        (account_order_events["event_type"] == "ORDER_DEFERRED").sum()
                    ),
                    "order_expired_count": int(
                        (account_order_events["event_type"] == "ORDER_EXPIRED").sum()
                    ),
                    "order_cancelled_count": int(
                        (account_order_events["event_type"] == "ORDER_CANCELLED").sum()
                    ),
                    "pending_order_attempt_count": pending_attempt_count,
                    "pending_order_age_days": pending_order_age_days,
                    "attribution_total_pnl": float(
                        account_attribution["equity_change"].sum()
                    ),
                    "attribution_holding_price_pnl": float(
                        account_attribution["overnight_pnl"].sum()
                        + account_attribution["existing_position_intraday_pnl"].sum()
                    ),
                    "attribution_trade_timing_pnl": float(
                        account_attribution["trade_timing_pnl"].sum()
                    ),
                    "attribution_transaction_cost": float(
                        account_attribution["transaction_cost_contribution"].sum()
                    ),
                    "attribution_cash_distribution": float(
                        account_attribution["cash_distribution"].sum()
                    ),
                    "attribution_share_adjustment_bridge": float(
                        account_attribution["share_adjustment_bridge"].sum()
                    ),
                    "attribution_residual": float(account_attribution["residual"].sum()),
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
                    "share_adjustment_count": int(len(applied_adjustments)),
                },
            }
        )
    return {
        "report_schema_version": PAPER_REPORT_SCHEMA_VERSION,
        "risk_policy": {
            **asdict(risk_policy),
            "scope": "extended_paper_observation_only",
            "authorizes_real_money": False,
        },
        "observation_progress": asdict(observation_progress),
        "forward_benchmark": asdict(forward_benchmark),
        "portfolio_id": result.portfolio_id,
        "operation_status": result.status,
        "processed_dates": [item.isoformat() for item in result.processed_dates],
        "data_context": _data_context_payload(result.data_context),
        "accounts": accounts,
        "daily_rows": int(len(daily)),
        "observation_history_rows": int(len(observation_history)),
        "trade_rows": int(len(trades)),
        "order_event_rows": int(len(order_events)),
        "attribution_rows": int(len(attribution)),
        "distribution_rows": int(len(distributions)),
        "share_adjustment_rows": int(len(share_adjustments)),
        "disclaimer": "模拟盘、非实盘、非投资建议；费用与滑点均为假设情景。",
        "limitations": [
            "不连接券商，不发送真实订单。",
            "固定滑点开盘成交是模拟假设，不代表实际成交结果。",
            "待成交买单最多尝试 3 次；资金不足会延期，第三次失败会过期。",
            "交易时点贡献只表示相对当日开盘不交易的机械差异，不代表策略 alpha。",
            "份额调整桥接项用于连接不复权价格与新份额，不应单独解释为投资收益。",
            "现金分红只接受本地可审计快照；缺失、格式错误或迟到快照不会被估算。",
            "份额调整只支持同代码且账户结果为整数份；跨代码派送和零碎份额分配会被拒绝。",
            "暂未建模停牌和部分成交。",
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
    observation_history_csv_name: str,
    trades_csv_name: str,
    order_events_csv_name: str,
    attribution_csv_name: str,
    distributions_csv_name: str,
    share_adjustments_csv_name: str,
) -> str:
    raw_accounts = payload["accounts"]
    raw_risk_policy = payload["risk_policy"]
    raw_observation_progress = payload["observation_progress"]
    raw_forward_benchmark = payload["forward_benchmark"]
    assert isinstance(raw_accounts, list)
    assert isinstance(raw_risk_policy, dict)
    assert isinstance(raw_observation_progress, dict)
    assert isinstance(raw_forward_benchmark, dict)
    stage_labels = {
        "initial_observation": "初始观察",
        "twenty_day_observation": "达到 20 日观察",
        "sixty_day_observation": "达到 60 日观察",
        "one_hundred_twenty_day_observation": "达到 120 日观察",
        "one_year_observation": "达到约一交易年观察",
    }
    raw_reached = raw_observation_progress["reached_milestones"]
    assert isinstance(raw_reached, (list, tuple))
    reached_text = "、".join(str(int(item)) for item in raw_reached) or "尚无"
    next_milestone = raw_observation_progress["next_milestone"]
    if next_milestone is None:
        next_checkpoint_text = "固定检查点已全部达到"
    else:
        next_checkpoint_text = (
            f"下一个检查点为 {int(next_milestone)} 个收益观察，尚需 "
            f"{int(raw_observation_progress['observations_to_next_milestone'])} 个"
        )
    minimum_evidence_observations = int(
        raw_observation_progress["minimum_evidence_observations"]
    )
    risk_policy_summary = (
        f"至少 {int(raw_risk_policy['minimum_return_observations'])} 个收益观察；"
        f"最大回撤不差于 -{float(raw_risk_policy['maximum_drawdown']):.0%}；"
        "年化波动率不高于 "
        f"{float(raw_risk_policy['maximum_annualized_volatility']):.0%}；"
        "最长连续亏损不超过 "
        f"{int(raw_risk_policy['maximum_consecutive_losing_days'])} 天。"
    )
    account_rows: list[str] = []
    attribution_rows: list[str] = []
    risk_rows: list[str] = []
    benchmark_rows: list[str] = []
    lineage_blocks: list[str] = []
    for raw_account in raw_accounts:
        assert isinstance(raw_account, dict)
        state = raw_account["state"]
        metrics = raw_account["metrics"]
        risk = raw_account["risk"]
        relative = raw_account["relative_performance"]
        latest_run = raw_account["latest_run"]
        assert isinstance(state, dict)
        assert isinstance(metrics, dict)
        assert isinstance(risk, dict)
        assert isinstance(relative, dict)
        assert isinstance(latest_run, dict)
        pending_order = state["pending_order"]
        if isinstance(pending_order, dict):
            pending_order_label = (
                f"{pending_order.get('action')}｜信号日 {pending_order.get('signal_date')}｜"
                f"已尝试 {metrics['pending_order_attempt_count']} 次｜"
                f"年龄 {metrics['pending_order_age_days']} 天"
            )
        else:
            pending_order_label = "无"
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
            f"<td>{int(metrics['order_deferred_count'])}/"
            f"{int(metrics['order_expired_count'])}/"
            f"{int(metrics['order_cancelled_count'])}</td>"
            f"<td>{float(metrics['pending_distribution_cash']):,.2f}</td>"
            f"<td>{float(metrics['distribution_cash_total']):,.2f}</td>"
            f"<td>{int(metrics['share_adjustment_count'])}</td>"
            f"<td>{html.escape(pending_order_label)}</td>"
            "</tr>"
        )
        attribution_rows.append(
            "<tr>"
            f"<td>{html.escape(str(raw_account['account_id']))}</td>"
            f"<td>{float(metrics['attribution_total_pnl']):,.2f}</td>"
            f"<td>{float(metrics['attribution_holding_price_pnl']):,.2f}</td>"
            f"<td>{float(metrics['attribution_trade_timing_pnl']):,.2f}</td>"
            f"<td>{float(metrics['attribution_transaction_cost']):,.2f}</td>"
            f"<td>{float(metrics['attribution_cash_distribution']):,.2f}</td>"
            f"<td>{float(metrics['attribution_share_adjustment_bridge']):,.2f}</td>"
            f"<td>{float(metrics['attribution_residual']):,.8f}</td>"
            "</tr>"
        )
        status_labels = {
            "insufficient_history": "历史不足",
            "risk_limit_breached": "风险门槛超限",
            "extended_paper_observation": "仅允许继续模拟观察",
        }
        raw_breaches = risk["breach_codes"]
        assert isinstance(raw_breaches, list)
        breach_labels = {
            "MAX_DRAWDOWN": "最大回撤",
            "ANNUALIZED_VOLATILITY": "年化波动率",
            "LOSING_STREAK": "连续亏损日",
        }
        breach_text = "、".join(
            breach_labels.get(str(code), str(code)) for code in raw_breaches
        ) or "无"
        risk_rows.append(
            "<tr>"
            f"<td>{html.escape(str(raw_account['account_id']))}</td>"
            f"<td>{int(risk['return_observations'])}</td>"
            f"<td>{_optional_percent(risk['annualized_volatility'])}</td>"
            f"<td>{float(risk['max_drawdown']):.2%}</td>"
            f"<td>{float(risk['current_drawdown']):.2%}</td>"
            f"<td>{_optional_percent(risk['worst_daily_return'])}</td>"
            f"<td>{int(risk['losing_days'])}</td>"
            f"<td>{int(risk['max_consecutive_losing_days'])}</td>"
            f"<td>{float(risk['average_exposure']):.2%}</td>"
            f"<td>{float(risk['current_exposure']):.2%}</td>"
            f"<td>{html.escape(breach_text)}</td>"
            f"<td>{html.escape(status_labels[str(risk['gate_status'])])}</td>"
            "</tr>"
        )
        evidence_status_labels = {
            "insufficient_history": "历史不足",
            "risk_limit_breached": "风险门槛超限",
            "did_not_beat_cash": "未跑赢现金基准",
            "did_not_beat_asset_price": "未跑赢标的价格基准",
            "extended_paper_observation": "仅允许继续模拟观察",
        }
        benchmark_rows.append(
            "<tr>"
            f"<td>{html.escape(str(raw_account['account_id']))}</td>"
            f"<td>{float(relative['account_total_return']):.2%}</td>"
            f"<td>{float(relative['cash_benchmark_return']):.2%}</td>"
            f"<td>{float(relative['return_difference_vs_cash']):.2%}</td>"
            f"<td>{float(relative['asset_price_benchmark_return']):.2%}</td>"
            f"<td>{float(relative['return_difference_vs_asset_price']):.2%}</td>"
            f"<td>{'是' if bool(relative['beat_cash']) else '否'}</td>"
            f"<td>{'是' if bool(relative['beat_asset_price']) else '否'}</td>"
            f"<td>{html.escape(evidence_status_labels[str(relative['gate_status'])])}</td>"
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
  <th>权益</th><th>累计收益</th><th>当前回撤</th><th>成交边数</th><th>延期/过期/取消</th><th>待到账分红</th>
<th>累计分红到账</th><th>份额调整次数</th><th>待成交订单</th></tr></thead>
<tbody>{''.join(account_rows)}</tbody></table>
<h2>前向观察进度</h2>
<p class="warning">样本积累进度不等于策略有效，也不授权投入真实资金。</p>
<p class="note">共同前向期间 {html.escape(str(raw_observation_progress['start_date']))} 至
{html.escape(str(raw_observation_progress['end_date']))}，已有
{int(raw_observation_progress['return_observations'])} 个收益观察；当前阶段：
{html.escape(stage_labels[str(raw_observation_progress['stage'])])}。
固定里程碑为 20、60、120、252 个收益观察；已达到：{html.escape(reached_text)}；
{html.escape(next_checkpoint_text)}。最小 {minimum_evidence_observations} 日观察窗完成度为
{float(raw_observation_progress['minimum_window_completion']):.0%}。</p>
<h2>风险仪表盘</h2>
<p class="warning">固定门槛只用于判断是否值得延长模拟观察，不授权投入真实资金，
也不证明策略未来盈利。</p>
<p class="note">{html.escape(risk_policy_summary)}</p>
<table><thead><tr><th>账户</th><th>收益观察数</th><th>年化波动率</th><th>最大回撤</th>
<th>当前回撤</th><th>最差单日</th><th>亏损天数</th><th>最长连亏</th><th>平均仓位</th><th>当前仓位</th>
<th>超限项</th><th>模拟观察门禁</th></tr></thead><tbody>{''.join(risk_rows)}</tbody></table>
<h2>前向基准对比</h2>
<p class="note">共同期间 {html.escape(str(raw_forward_benchmark['start_date']))} 至
{html.escape(str(raw_forward_benchmark['end_date']))}，共
{int(raw_forward_benchmark['return_observations'])} 个收益观察；现金基准固定为 0%。
不复权价格基准收益为 {float(raw_forward_benchmark['total_return']):.2%}，不包含现金分红和份额调整，
不是完整总回报。表中差异是百分点差，不是策略 alpha。</p>
<table><thead><tr><th>账户</th><th>账户收益</th><th>现金基准</th><th>相对现金</th>
<th>不复权价格基准</th><th>相对价格基准</th><th>跑赢现金</th><th>跑赢价格基准</th>
<th>研究证据门禁</th></tr></thead>
<tbody>{''.join(benchmark_rows)}</tbody></table>
<h2>收益归因（元）</h2>
<p class="note">交易时点贡献是相对“当日开盘不交易”的机械差异；份额调整桥接项用于对齐不复权价格，
两者都不等同于策略 alpha。所有分项必须与权益变化逐日对账。</p>
<table><thead><tr><th>账户</th><th>权益变化</th><th>持仓价格</th><th>交易时点</th>
<th>交易成本</th><th>现金分红</th><th>份额调整桥</th><th>残差</th></tr></thead>
<tbody>{''.join(attribution_rows)}</tbody></table>
<img src="{html.escape(chart_name)}" alt="两个模拟账户的净值和回撤图">
  <p><a href="{html.escape(daily_csv_name)}">逐日权益 CSV</a>；
  <a href="{html.escape(observation_history_csv_name)}">逐日观察历史 CSV</a>；
  <a href="{html.escape(trades_csv_name)}">成交明细 CSV</a>；
  <a href="{html.escape(order_events_csv_name)}">订单生命周期 CSV</a>；
  <a href="{html.escape(attribution_csv_name)}">收益归因 CSV</a>；
<a href="{html.escape(distributions_csv_name)}">分红明细 CSV</a>；
<a href="{html.escape(share_adjustments_csv_name)}">份额调整 CSV</a></p>
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


def _optional_percent(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("报告中的风险百分比不是有限数字")
    return f"{value:.2%}"
