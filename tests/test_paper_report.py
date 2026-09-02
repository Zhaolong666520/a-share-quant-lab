from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import finance_lab.paper_report as paper_report_module
from finance_lab.paper_pipeline import paper_init_portfolio
from finance_lab.paper_report import write_paper_report
from finance_lab.paper_risk import DEFAULT_PAPER_RISK_POLICY
from tests.paper_helpers import make_paper_test_project, write_update_summary


def _initialized_result(tmp_path: Path):
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    as_of_date = prices["trade_date"].max().date()
    write_update_summary(
        paths,
        end=as_of_date.isoformat(),
        rows=len(prices),
        curated_file="data/curated/sh_510300.parquet",
        source_errors={"akshare": "<script>alert('bad')</script>"},
    )
    return paths, paper_init_portfolio(root=tmp_path, as_of_date=as_of_date)


def test_paper_report_writes_safe_auditable_outputs(tmp_path: Path) -> None:
    paths, result = _initialized_result(tmp_path)

    outputs = write_paper_report(result, paths)

    for output_path in outputs.all_paths:
        assert output_path.exists()
        assert output_path.resolve().is_relative_to(paths.outputs.resolve())
    payload = json.loads(outputs.archive_json.read_text(encoding="utf-8"))
    document = outputs.archive_html.read_text(encoding="utf-8")
    assert len(payload["accounts"]) == 2
    assert payload["report_schema_version"] == 3
    assert payload["risk_policy"] == {
        "minimum_return_observations": 60,
        "maximum_drawdown": 0.15,
        "maximum_annualized_volatility": 0.3,
        "maximum_consecutive_losing_days": 5,
        "scope": "extended_paper_observation_only",
        "authorizes_real_money": False,
    }
    assert "模拟盘" in document
    assert "非实盘" in document
    assert "非投资建议" in document
    assert "<script>alert('bad')</script>" not in document
    assert "&lt;script&gt;" in document
    assert {state.account_id for state in result.states} == {
        item["account_id"] for item in payload["accounts"]
    }
    assert outputs.order_events_csv.exists()
    assert outputs.attribution_csv.exists()
    assert payload["order_event_rows"] == 2
    assert payload["attribution_rows"] == 2
    assert all(
        account["metrics"]["order_created_count"] == 1
        and account["metrics"]["pending_order_attempt_count"] == 0
        and account["metrics"]["pending_order_age_days"] == 0
        for account in payload["accounts"]
    )
    assert payload["forward_benchmark"] == {
        "kind": "unadjusted_close_price_only",
        "start_date": result.states[0].last_trade_date.isoformat(),
        "end_date": result.states[0].last_trade_date.isoformat(),
        "valuation_observations": 1,
        "return_observations": 0,
        "start_close": pytest.approx(result.states[0].last_close),
        "end_close": pytest.approx(result.states[0].last_close),
        "total_return": pytest.approx(0.0),
        "annualized_volatility": None,
        "max_drawdown": pytest.approx(0.0),
        "includes_cash_distributions": False,
        "includes_share_adjustments": False,
    }
    assert all(
        account["relative_performance"]["account_total_return"]
        == pytest.approx(0.0)
        and account["relative_performance"]["return_difference_vs_cash"]
        == pytest.approx(0.0)
        and account["relative_performance"]["return_difference_vs_asset_price"]
        == pytest.approx(0.0)
        and account["relative_performance"]["gate_status"]
        == "insufficient_history"
        and not account["relative_performance"]["gate_passed"]
        for account in payload["accounts"]
    )
    assert all(
        account["metrics"]["attribution_total_pnl"] == pytest.approx(0.0)
        and account["metrics"]["attribution_residual"] == pytest.approx(0.0)
        for account in payload["accounts"]
    )
    assert all(
        account["risk"]["valuation_observations"] == 1
        and account["risk"]["return_observations"] == 0
        and account["risk"]["annualized_volatility"] is None
        and account["risk"]["max_drawdown"] == pytest.approx(0.0)
        and account["risk"]["gate_status"] == "insufficient_history"
        and not account["risk"]["gate_passed"]
        for account in payload["accounts"]
    )
    assert "订单生命周期 CSV" in document
    assert "收益归因 CSV" in document
    assert "收益归因（元）" in document
    assert "风险仪表盘" in document
    assert "历史不足" in document
    assert "亏损天数" in document
    assert "60 个收益观察" in document
    assert "最大回撤不差于 -15%" in document
    assert "年化波动率不高于 30%" in document
    assert "最长连续亏损不超过 5 天" in document
    assert "不授权投入真实资金" in document
    assert "前向基准对比" in document
    assert "现金基准" in document
    assert "不复权价格基准" in document
    assert "跑赢现金" in document
    assert "百分点差" in document


def test_paper_report_keeps_archive_identity_and_recovers_missing_file(tmp_path: Path) -> None:
    paths, result = _initialized_result(tmp_path)
    first = write_paper_report(result, paths)
    first.archive_json.write_text("preserved archive", encoding="utf-8")
    first.archive_html.unlink()

    second = write_paper_report(result, paths)

    assert second.archive_json == first.archive_json
    assert second.archive_json.read_text(encoding="utf-8") == "preserved archive"
    assert second.archive_html == first.archive_html
    assert second.archive_html.exists()


def test_paper_report_archive_identity_includes_the_risk_policy(tmp_path: Path) -> None:
    paths, result = _initialized_result(tmp_path)
    first = write_paper_report(result, paths)
    stricter = replace(DEFAULT_PAPER_RISK_POLICY, maximum_drawdown=0.10)

    second = write_paper_report(result, paths, risk_policy=stricter)

    assert second.archive_json != first.archive_json
    second_payload = json.loads(second.archive_json.read_text(encoding="utf-8"))
    assert second_payload["risk_policy"]["maximum_drawdown"] == pytest.approx(0.10)


def test_paper_report_archive_identity_includes_the_report_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, result = _initialized_result(tmp_path)
    first = write_paper_report(result, paths)
    next_version = paper_report_module.PAPER_REPORT_SCHEMA_VERSION + 1
    monkeypatch.setattr(
        paper_report_module,
        "PAPER_REPORT_SCHEMA_VERSION",
        next_version,
    )

    second = write_paper_report(result, paths)

    assert second.archive_json != first.archive_json
    second_payload = json.loads(second.archive_json.read_text(encoding="utf-8"))
    assert second_payload["report_schema_version"] == next_version


def test_paper_report_rejects_unsafe_portfolio_output_name(tmp_path: Path) -> None:
    paths, result = _initialized_result(tmp_path)

    with pytest.raises(ValueError, match="账户组"):
        write_paper_report(replace(result, portfolio_id="../escape"), paths)
