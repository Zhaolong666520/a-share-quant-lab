"""Synthetic saved source comparisons; no provider calls or source-policy changes."""
from __future__ import annotations

import json

import pytest

from finance_lab.paper_operational_status import inspect_paper_operational_status
from tests.test_paper_automation_history import archive, payload
from tests.test_paper_operational_status import fingerprint, ledger, market


def prepared(root, comparison, *, symbol="sh.510300", windows=None):
    archive(root, payload())
    ledger(root)
    record = {"symbol": symbol, "rows": 2131, "source_errors": {}}
    if windows is not None:
        record.update(primary_source="tencent", source_request_windows=windows)
    if comparison is not None:
        record["source_comparison"] = comparison
    market(
        root,
        summary={"status": "success", "successful_instruments": 1, "failed_sources": 0,
                 "records": [record]},
    )


@pytest.mark.parametrize(
    "comparison",
    [None, {"status": "pass"},
     {"status": "warning", "common_rows": 189, "primary_only": 1942,
      "secondary_only": 0, "coverage_ratio": 189 / 2131, "message": "private-provider-message"},
     {"status": "warning", "common_rows": 0, "primary_only": 2131, "secondary_only": 3},
     {"status": "warning"}],
)
def test_recorded_warning_is_attention_without_claiming_fetch_failure(tmp_path, comparison):
    prepared(tmp_path, comparison)
    before = fingerprint(tmp_path)
    result = inspect_paper_operational_status(tmp_path, "study-1")
    assert fingerprint(tmp_path) == before
    assert result["market"]["source_outcome"] == "success"
    assert result["market"]["failed_sources"] == 0
    warning = comparison is not None and comparison["status"] == "warning"
    assert ("source_comparison_warning" in {d["code"] for d in result["diagnostics"]}) is warning
    assert result["command_exit_code"] == int(warning)
    assert "private-provider-message" not in json.dumps(result)
    if warning:
        assert result["market"]["source_comparison_warning_count"] == 1
        observed = result["market"]["source_comparison_warnings"][0]
        assert observed["symbol"] == "sh.510300"
        assert observed["coverage_ratio"] == comparison.get("coverage_ratio")
    else:
        assert result["market"]["source_comparison_state"] == (
            "unknown" if comparison is None else "pass"
        )


@pytest.mark.parametrize(
    "field,value",
    [("common_rows", True), ("primary_only", -1), ("secondary_only", "3"),
     ("coverage_ratio", True), ("coverage_ratio", -0.1), ("coverage_ratio", 1.1),
     ("coverage_ratio", "0.1"), ("coverage_ratio", 10**400), ("status", "private-status")],
)
def test_malformed_comparison_never_claims_success_or_leaks_details(tmp_path, field, value):
    comparison = {"status": "warning", field: value}
    prepared(tmp_path, comparison)
    before = fingerprint(tmp_path)
    result = inspect_paper_operational_status(tmp_path, "study-1")
    assert fingerprint(tmp_path) == before
    assert result["market"]["source_outcome"] == "unknown"
    assert result["market"]["source_comparison_state"] == "unknown"
    assert result["command_exit_code"] == 1
    assert "summary_conflict" in {item["code"] for item in result["diagnostics"]}
    assert "private-status" not in json.dumps(result)


@pytest.mark.parametrize("symbol", ["private-symbol-token", "sh.５１０３００"])
def test_invalid_comparison_symbol_does_not_reach_output(tmp_path, symbol):
    prepared(tmp_path, {"status": "warning"}, symbol=symbol)
    result = inspect_paper_operational_status(tmp_path, "study-1")
    assert result["market"]["source_outcome"] == "unknown"
    assert symbol not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("stage", ["query", "recorded"])
def test_comparison_warning_is_withdrawn_if_summary_changes_during_query(
    tmp_path, monkeypatch, stage
):
    import finance_lab.paper_runtime_status as runtime
    from tests.test_paper_runtime_status import NOW, scheduler

    prepared(tmp_path, {"status": "warning", "coverage_ratio": 0.1}, windows=request_windows())

    def change_summary():
        path = tmp_path / "outputs/update_summary.json"
        path.write_text(path.read_text() + " ")

    def changed(*args):
        change_summary()
        return scheduler()

    if stage == "query":
        monkeypatch.setattr(runtime, "read_scheduler_status", changed)
    else:
        from finance_lab.paper_store import PaperStore

        original = PaperStore.audit_recovery_ledger

        def audited(self):
            result = original(self)
            change_summary()
            return result

        monkeypatch.setattr(PaperStore, "audit_recovery_ledger", audited)
        monkeypatch.setattr(runtime, "read_scheduler_status", lambda *a: scheduler())
    result = runtime.inspect_paper_runtime_status(tmp_path, "study-1", "2026-01-02", now=NOW)
    assert result["market_update"]["state"] == "changed"
    assert result["recorded_status"]["market"]["source_comparison_state"] == "unknown"
    assert result["recorded_status"]["market"]["source_comparison_warnings"] == []
    assert result["recorded_status"]["market"]["source_comparison_warning_count"] is None
    assert "source_comparison_warning" not in {item["code"] for item in result["diagnostics"]}
    assert "request_window_context" not in json.dumps(result)


def request_windows(start="2026-01-01", end="2026-10-22", primary="tencent"):
    return {
        primary: {"start_date": "2018-01-01", "end_date": "2026-10-22", "role": "primary"},
        "baostock": {"start_date": start, "end_date": end, "role": "reference"},
    }


@pytest.mark.parametrize("primary", ["tencent", "akshare"])
@pytest.mark.parametrize("start,end,relation", [
    ("2026-01-01", "2026-10-22", "shorter"),
    ("2018-01-01", "2026-09-30", "shorter"),
    ("2018-01-01", "2026-10-22", "same"),
    ("2017-01-01", "2026-09-30", "different"),
    ("2017-01-01", "2027-01-01", "different"),
    ("2027-01-01", "2027-02-01", "different"),
])
def test_request_window_scope_never_substitutes_window_coverage(primary, start, end, relation):
    from finance_lab.paper_operational_status import _comparison

    windows = request_windows(start, end, primary)
    windows[primary]["private"] = "private-provider-token"
    record = {"symbol": "sh.510300", "primary_source": primary,
              "source_request_windows": windows,
              "source_comparison": {"status": "warning", "coverage_ratio": 192 / 2134,
                                    "message": "private-provider-token"}}
    result = _comparison(record)
    assert result is not None
    context = result["request_window_context"]
    assert result["status"] == "warning" and result["coverage_ratio"] == 192 / 2134
    assert result["coverage_scope"] == "all_returned_rows"
    assert context["state"] == "available" and context["relation"] == relation
    assert context["primary"] == {
        "source": primary, "start_date": "2018-01-01", "end_date": "2026-10-22",
    }
    assert context["reference"] == {"source": "baostock", "start_date": start, "end_date": end}
    assert "窗口内逐日覆盖未计算" in context["explanation"]
    assert "private-provider-token" not in json.dumps(result)


@pytest.mark.parametrize("bad", [None, [], True, "private-provider-token", {},
                                {"private-provider-token": {}}])
def test_request_window_bad_container_is_unknown_only(bad):
    from finance_lab.paper_operational_status import _comparison

    for field in ("primary_source", "source_request_windows"):
        record = {"primary_source": "tencent", "source_request_windows": request_windows(),
                  "source_comparison": {"status": "warning", "coverage_ratio": .1}}
        record[field] = bad
        result = _comparison(record)
        assert result is not None
        assert result["status"] == "warning" and result["coverage_ratio"] == .1
        assert result["request_window_context"]["state"] == "unknown"
        assert result["request_window_context"]["primary"] is None
        assert result["request_window_context"]["reference"] is None
        assert "private-provider-token" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [
    ("start_date", "2026-02-30"), ("start_date", "20260101"),
    ("start_date", "２０２６-01-01"), ("end_date", "2026-10-22T00:00:00"),
    ("start_date", "2027-01-01"), ("end_date", None), ("start_date", []),
    ("role", "primary"), ("role", "private-provider-token"),
])
def test_request_window_malformed_metadata_keeps_warning_exit_and_readonly(tmp_path, field, value):
    windows = request_windows()
    windows["baostock"][field] = value
    prepared(tmp_path, {"status": "warning", "coverage_ratio": .1}, windows=windows)
    before = fingerprint(tmp_path)
    result = inspect_paper_operational_status(tmp_path, "study-1")
    assert fingerprint(tmp_path) == before
    assert result["command_exit_code"] == 1
    assert result["market"]["source_outcome"] == "success"
    assert result["market"]["source_comparison_state"] == "warning"
    item = result["market"]["source_comparison_warnings"][0]
    assert item["coverage_ratio"] == .1 and item["request_window_context"]["state"] == "unknown"
    assert "private-provider-token" not in json.dumps(result)


def test_request_window_missing_context_preserves_legacy_and_unknown_ratio():
    from finance_lab.paper_operational_status import _comparison

    result = _comparison({"source_comparison": {"status": "warning"}})
    assert result is not None
    assert result["coverage_ratio"] is None
    assert result["request_window_context"]["state"] == "unknown"


def test_request_window_valid_context_keeps_warning_exit_and_readonly(tmp_path):
    prepared(tmp_path, {"status": "warning", "coverage_ratio": .1}, windows=request_windows())
    before = fingerprint(tmp_path)
    result = inspect_paper_operational_status(tmp_path, "study-1")
    assert fingerprint(tmp_path) == before
    assert result["command_exit_code"] == 1
    assert result["market"]["source_outcome"] == "success"
    item = result["market"]["source_comparison_warnings"][0]
    assert item["coverage_ratio"] == .1
    assert item["request_window_context"]["relation"] == "shorter"
