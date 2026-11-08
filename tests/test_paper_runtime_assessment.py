"""Synthetic timing/report evidence only; no installed task, network or business DB."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from finance_lab.operational_status_io import Observation
from finance_lab.paper_runtime_assessment import assess_start, inspect_report_json
from finance_lab.paper_runtime_evidence import moment
from tests.test_paper_runtime_status import base, scheduler

PLANNED = moment("2026-10-26T18:30:00+08:00")
AFTER = moment("2026-10-26T20:00:00+08:00")


def sample(root: Path):
    health = {
        "as_of_date": "2026-10-26",
        "dataset_id": "dataset-one",
        "curated_sha256": "curated-one",
        "data_end_date": "2026-10-26",
    }
    refs, accounts = [], []
    for number in (1, 2):
        ref = {
            "account_id": f"account-{number}",
            "run_id": f"paper-run-{number:024x}",
            "batch_id": "daily-batch",
            "trade_date": "2026-10-26",
            "dataset_id": health["dataset_id"],
            "curated_sha256": health["curated_sha256"],
            "state_hash": str(number) * 64,
        }
        refs.append(ref)
        accounts.append(
            {
                "account_id": ref["account_id"],
                "state": {"account_id": ref["account_id"], "last_trade_date": "2026-10-26"},
                "latest_run": {**ref, "data_health": copy.deepcopy(health)},
            }
        )
    report = {
        "portfolio_id": "study-1",
        "operation_status": "processed",
        "processed_dates": ["2026-10-26"],
        "data_context": health,
        "accounts": accounts,
    }
    latest = {
        **base()["automation"]["latest_attempt"],
        "started_at": "2026-10-26T10:30:13Z",
        "completed_at": "2026-10-26T10:30:42Z",
        "processed_dates": ["2026-10-26"],
        "run_identity": {
            "health": {"dataset_id": health["dataset_id"], "as_of_date": "2026-10-26"},
            "ledger": {"scope": "complete", "runs": refs},
            "report": {"archive_json_name": "study-1_report.json", "archive_json_sha256": ""},
        },
    }
    write_report(root, latest, report)
    return latest, report


def write_report(root, latest, report):
    path = root / "outputs/study-1_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    latest["run_identity"]["report"]["archive_json_sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


def inspect(root, latest):
    return inspect_report_json(root, "study-1", latest, Observation(root))


@pytest.mark.parametrize(
    "start,origin,state,delay",
    [
        ("2026-10-26T10:30:00Z", "time_trigger", "on_time", 0),
        ("2026-10-26T19:01:10+08:00", "time_trigger", "delayed", 1870),
        ("2026-10-26T19:01:10+08:00", "manual", "unknown", None),
        ("2026-10-26T19:01:10+08:00", "unknown", "unknown", None),
        ("2026-10-26T18:29:59+08:00", "time_trigger", "start_not_observed", None),
        ("2026-10-23T18:30:00+08:00", "time_trigger", "start_not_observed", None),
        (None, "time_trigger", "start_not_observed", None),
        ("2026-10-27T18:30:00+08:00", "time_trigger", "unknown", None),
    ],
)
def test_explicit_start_does_not_conflate_origin_or_other_dates(start, origin, state, delay):
    observed = scheduler(last_run_time=start, trigger_origin=origin)
    result = assess_start(observed, PLANNED, moment("2026-10-27T20:00:00+08:00"))
    assert result["state"] == state
    assert result["delay_seconds"] == delay
    assert result["scheduler_application_correlation"] == "not_verified"


def test_not_due_does_not_require_available_scheduler():
    result = assess_start({}, PLANNED, moment("2026-10-26T18:29:59+08:00"))
    assert result["state"] == "not_due"
    assert result["delay_seconds"] is None
    assert assess_start({}, None, AFTER)["state"] == "unknown"
    assert assess_start({}, PLANNED, AFTER)["reason"] == "scheduler_unavailable"


def test_bound_report_checks_final_receipts_and_actual_data_date(tmp_path):
    latest, report = sample(tmp_path)
    result = inspect(tmp_path, latest)
    assert result["state"] == "matched"
    assert result["checked_receipts"] == 2
    assert result["data_end_date"] == "2026-10-26"
    assert not result["database_events_verified"]
    assert not result["other_report_artifacts_verified"]
    # The request date may be later than the actual market date.
    latest["run_identity"]["health"]["as_of_date"] = "2026-10-27"
    report["data_context"]["as_of_date"] = "2026-10-27"
    for account in report["accounts"]:
        account["latest_run"]["data_health"]["as_of_date"] = "2026-10-27"
    write_report(tmp_path, latest, report)
    assert inspect(tmp_path, latest)["data_end_date"] == "2026-10-26"


@pytest.mark.parametrize(
    "variant",
    [
        "portfolio",
        "processed",
        "dataset",
        "state_date",
        "receipt",
        "duplicate_account",
        "missing_account",
        "future_data",
        "different_account_data",
        "operation",
        "duplicate_key",
    ],
)
def test_even_hash_matching_conflicting_report_cannot_pass(tmp_path, variant):
    latest, report = sample(tmp_path)
    if variant == "portfolio":
        report["portfolio_id"] = "different"
    elif variant == "processed":
        report["processed_dates"] = ["2026-10-23"]
    elif variant == "dataset":
        report["data_context"]["dataset_id"] = "another"
    elif variant == "state_date":
        report["accounts"][0]["state"]["last_trade_date"] = "2026-10-23"
    elif variant == "receipt":
        report["accounts"][0]["latest_run"]["state_hash"] = "0" * 64
    elif variant == "duplicate_account":
        report["accounts"].append(copy.deepcopy(report["accounts"][0]))
    elif variant == "missing_account":
        report["accounts"].pop()
    elif variant == "future_data":
        report["data_context"]["data_end_date"] = "2026-10-27"
    elif variant == "different_account_data":
        report["accounts"][0]["latest_run"]["data_health"]["data_end_date"] = "2026-10-23"
    elif variant == "operation":
        report["operation_status"] = "no-op"
    write_report(tmp_path, latest, report)
    if variant == "duplicate_key":
        path = tmp_path / "outputs/study-1_report.json"
        path.write_text('{"portfolio_id":"study-1","portfolio_id":"different"}')
        latest["run_identity"]["report"]["archive_json_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    result = inspect(tmp_path, latest)
    assert result["state"] != "matched"
    assert result["data_end_date"] is None
    assert not result["content_identity_matched"]


@pytest.mark.parametrize("variant", ["missing", "tampered", "oversized", "traversal", "old_schema"])
def test_unavailable_report_never_falls_back_to_latest_path(tmp_path, variant):
    latest, report = sample(tmp_path)
    if variant == "missing":
        latest["run_identity"]["report"]["archive_json_name"] = "absent.json"
    elif variant == "tampered":
        (tmp_path / "outputs/study-1_report.json").write_text("{}")
    elif variant == "oversized":
        (tmp_path / "outputs/study-1_report.json").write_bytes(b" " * (2 * 1024**2 + 1))
    elif variant == "traversal":
        latest["run_identity"]["report"]["archive_json_name"] = "../private.json"
    else:
        latest["run_identity"] = None
    assert inspect(tmp_path, latest)["state"] != "matched"


def test_multiday_only_checks_last_day_and_noop_has_no_new_receipts(tmp_path):
    latest, report = sample(tmp_path)
    old_refs = copy.deepcopy(latest["run_identity"]["ledger"]["runs"])
    for i, ref in enumerate(old_refs):
        ref["trade_date"] = "2026-10-23"
        ref["run_id"] = f"paper-run-{i + 10:024x}"
        ref["batch_id"] = "previous-batch"
    latest["run_identity"]["ledger"]["runs"] = old_refs + latest["run_identity"]["ledger"]["runs"]
    latest["processed_dates"] = report["processed_dates"] = ["2026-10-23", "2026-10-26"]
    write_report(tmp_path, latest, report)
    result = inspect(tmp_path, latest)
    assert result["state"] == "matched"
    assert result["unchecked_earlier_receipts"] == 2
    latest["operation_status"] = report["operation_status"] = "no-op"
    latest["processed_dates"] = report["processed_dates"] = []
    latest["run_identity"]["ledger"]["runs"] = []
    write_report(tmp_path, latest, report)
    result = inspect(tmp_path, latest)
    assert result["state"] == "matched"
    assert result["checked_receipts"] == 0
    assert result["receipt_status"] == "not_applicable_no_new_receipts"


def runtime(root, monkeypatch, *, now=AFTER, expected="2026-10-26", planned=None):
    from finance_lab import paper_runtime_status as module

    latest, _ = sample(root)
    recorded = base()
    recorded["automation"] = {"latest_attempt": latest, "last_success": copy.deepcopy(latest)}
    recorded["ledger"]["last_valuation_date"] = "2026-10-26"
    monkeypatch.setattr(module, "inspect_paper_operational_status", lambda *a, **k: recorded)
    monkeypatch.setattr(
        module,
        "read_scheduler_status",
        lambda *a: scheduler(
            observed_at=now.isoformat(),
            last_run_time=PLANNED.isoformat(),
            trigger_origin="time_trigger",
        ),
    )
    return module.inspect_paper_runtime_status(
        root, "study-1", expected, now=now, task_name="Study Daily", expected_start_at=planned
    )


@pytest.mark.parametrize("variant", ["wrong_account", "future_state", "disagreeing_dates"])
def test_noop_still_requires_consistent_account_state(tmp_path, variant):
    latest, report = sample(tmp_path)
    latest["operation_status"] = report["operation_status"] = "no-op"
    latest["processed_dates"] = report["processed_dates"] = []
    latest["run_identity"]["ledger"]["runs"] = []
    state, run = report["accounts"][0]["state"], report["accounts"][0]["latest_run"]
    if variant == "wrong_account":
        state["account_id"] = "wrong"
    elif variant == "future_state":
        state["last_trade_date"] = run["trade_date"] = "2026-10-27"
    else:
        run["trade_date"] = "2026-10-23"
    write_report(tmp_path, latest, report)
    assert inspect(tmp_path, latest)["state"] != "matched"


def test_noop_allows_older_consistent_account_input_identity(tmp_path):
    latest, report = sample(tmp_path)
    latest["operation_status"] = report["operation_status"] = "no-op"
    latest["processed_dates"] = report["processed_dates"] = []
    latest["run_identity"]["ledger"]["runs"] = []
    latest["run_identity"]["health"] = {"dataset_id": "new-dataset", "as_of_date": "2026-10-27"}
    report["data_context"].update(
        dataset_id="new-dataset",
        curated_sha256="new-sha",
        as_of_date="2026-10-27",
        data_end_date="2026-10-27",
    )
    write_report(tmp_path, latest, report)
    result = inspect(tmp_path, latest)
    assert result["state"] == "matched"
    assert result["checked_receipts"] == 0
    assert result["receipt_status"] == "not_applicable_no_new_receipts"


@pytest.mark.parametrize("variant", ["missing_success", "missing_health", "future_health"])
def test_before_due_keeps_missing_or_invalid_dates_unknown(tmp_path, monkeypatch, variant):
    from finance_lab import paper_runtime_status as module

    recorded = base()
    recorded["ledger"]["last_valuation_date"] = None
    if variant == "missing_success":
        recorded["automation"]["last_success"] = None
    elif variant == "missing_health":
        recorded["automation"]["last_success"]["run_identity"] = None
    else:
        recorded["automation"]["last_success"]["run_identity"]["health"]["as_of_date"] = (
            "2026-10-28"
        )
        recorded["ledger"]["last_valuation_date"] = "2026-10-28"
    monkeypatch.setattr(module, "inspect_paper_operational_status", lambda *a, **k: recorded)
    result = module.inspect_paper_runtime_status(
        tmp_path,
        "study-1",
        "2026-10-27",
        now=moment("2026-10-27T14:00:00+08:00"),
        query_scheduler=False,
        expected_start_at="2026-10-27T18:30:00+08:00",
    )
    assert result["schedule"]["state"] == "not_due"
    assert result["progress"]["application"] == "unknown"
    assert result["progress"]["valuation"] == "unknown"
    assert "application_expected_date_unknown" in {v["code"] for v in result["diagnostics"]}


def test_runtime_before_due_suppresses_only_expected_date_lag(tmp_path, monkeypatch):
    result = runtime(
        tmp_path,
        monkeypatch,
        now=moment("2026-10-27T14:00:00+08:00"),
        expected="2026-10-27",
        planned="2026-10-27T18:30:00+08:00",
    )
    assert result["schedule"]["state"] == "not_due"
    assert result["progress"]["application"] == "not_due"
    assert result["progress"]["valuation"] == "not_due"
    codes = {item["code"] for item in result["diagnostics"]}
    assert (
        not {
            "application_behind_expected_date",
            "valuation_behind_expected_date",
            "scheduler_behind_expected_date",
            "expected_start_not_observed",
        }
        & codes
    )


def test_report_change_revokes_data_and_receipt_claims(tmp_path, monkeypatch):
    from finance_lab.paper_runtime_evidence import RuntimeObservation

    original = RuntimeObservation.changed

    def changed(self):
        (tmp_path / "outputs/study-1_report.json").write_text("{}")
        return original(self)

    monkeypatch.setattr(RuntimeObservation, "changed", changed)
    result = runtime(tmp_path, monkeypatch, planned=PLANNED.isoformat())
    assert "runtime_report" in result["observation"]["changed_sections"]
    assert result["report_json"]["state"] == "changed"
    assert result["report_json"]["data_end_date"] is None
    assert result["report_json"]["checked_receipts"] == 0
    assert not result["safety"]["natural_run_acceptance_verified"]


@pytest.mark.parametrize("planned", ["2026-10-26T18:30:00", "2026-10-25T18:30:00+08:00", "bad"])
def test_bad_plan_time_rejected_before_evidence_access(tmp_path, monkeypatch, planned):
    from finance_lab import paper_runtime_status as module

    monkeypatch.setattr(module, "read_runtime_evidence", lambda *a: pytest.fail("read evidence"))
    result = module.inspect_paper_runtime_status(
        tmp_path, "study-1", "2026-10-26", now=AFTER, expected_start_at=planned
    )
    assert result["command_exit_code"] == 2


def test_cli_passes_explicit_time(tmp_path, monkeypatch, capsys):
    from finance_lab import paper_runtime_status as module

    def inspect(*args, **kwargs):
        assert kwargs["expected_start_at"] == "2026-10-26T18:30:00+08:00"
        return {"command_exit_code": 1}

    monkeypatch.setattr(module, "inspect_paper_runtime_status", inspect)
    assert (
        module.main(
            [
                "--root",
                str(tmp_path),
                "--portfolio",
                "study-1",
                "--expected-as-of",
                "2026-10-26",
                "--expected-start-at",
                "2026-10-26T18:30:00+08:00",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["command_exit_code"] == 1


def test_future_report_dates_with_past_execution_times_are_revoked(tmp_path, monkeypatch):
    from finance_lab import paper_runtime_status as module

    latest, report = sample(tmp_path)
    latest["run_identity"]["health"]["as_of_date"] = "2026-10-27"
    latest["processed_dates"] = report["processed_dates"] = ["2026-10-27"]
    for ref in latest["run_identity"]["ledger"]["runs"]:
        ref["trade_date"] = "2026-10-27"
    report["data_context"].update(as_of_date="2026-10-27", data_end_date="2026-10-27")
    for account in report["accounts"]:
        account["state"]["last_trade_date"] = "2026-10-27"
        account["latest_run"]["trade_date"] = "2026-10-27"
        account["latest_run"]["data_health"].update(
            as_of_date="2026-10-27", data_end_date="2026-10-27"
        )
    write_report(tmp_path, latest, report)
    recorded = base()
    recorded["automation"] = {"latest_attempt": latest, "last_success": copy.deepcopy(latest)}
    monkeypatch.setattr(module, "inspect_paper_operational_status", lambda *a, **k: recorded)
    result = module.inspect_paper_runtime_status(
        tmp_path, "study-1", "2026-10-26", now=AFTER, query_scheduler=False
    )
    assert result["report_json"]["state"] == "unknown"
    assert result["report_json"]["data_end_date"] is None
    assert result["application"]["latest_archived_attempt"] is None
    assert "archive_request_date_future" in {v["code"] for v in result["diagnostics"]}
