from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from finance_lab.paper_pipeline import paper_init_portfolio
from finance_lab.paper_report import write_paper_report
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
    assert "模拟盘" in document
    assert "非实盘" in document
    assert "非投资建议" in document
    assert "<script>alert('bad')</script>" not in document
    assert "&lt;script&gt;" in document
    assert {state.account_id for state in result.states} == {
        item["account_id"] for item in payload["accounts"]
    }
    assert outputs.order_events_csv.exists()
    assert payload["order_event_rows"] == 2
    assert all(
        account["metrics"]["order_created_count"] == 1
        and account["metrics"]["pending_order_attempt_count"] == 0
        and account["metrics"]["pending_order_age_days"] == 0
        for account in payload["accounts"]
    )
    assert "订单生命周期 CSV" in document


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


def test_paper_report_rejects_unsafe_portfolio_output_name(tmp_path: Path) -> None:
    paths, result = _initialized_result(tmp_path)

    with pytest.raises(ValueError, match="账户组"):
        write_paper_report(replace(result, portfolio_id="../escape"), paths)
