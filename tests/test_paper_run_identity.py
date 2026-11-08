from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date, datetime

import pytest

from finance_lab.paper_automation import PaperAutomationActions, run_paper_daily_automation
from finance_lab.paper_models import PaperOperationResult
from finance_lab.paper_pipeline import paper_init_portfolio, paper_run_portfolio
from finance_lab.paper_store import PaperIdempotencyError, PaperStore
from tests.paper_helpers import make_paper_test_project
from tests.test_paper_automation_history import archive, payload
from tests.test_paper_pipeline import _append_rising_bars
from tests.test_paper_store import _accounts_and_initial_steps


def refs_json(refs):
    return [{**asdict(r), "trade_date": r.trade_date.isoformat()} for r in refs]


def inspect(root, portfolio="default"):
    from finance_lab.paper_operational_status import inspect_paper_operational_status

    return inspect_paper_operational_status(root, portfolio)


def automation(root, day, run, *, clock=None):
    return run_paper_daily_automation(
        root=root,
        as_of_date=day,
        actions=PaperAutomationActions(
            lambda **kw: {
                "status": "success",
                "successful_instruments": 1,
                "failed_sources": 0,
                "market_identity": {"batch_id": "a" * 32, "summary_sha256": "b" * 64},
            },
            lambda **kw: (
                root / "health.html",
                {
                    "health_status": "pass",
                    "dataset_id": "opaque-dataset",
                    "as_of_date": day.isoformat(),
                },
            ),
            run,
            paper_init_portfolio,
        ),
        clock=clock,
    )


def test_receipt_is_actual_rows_and_duplicate_does_not_create_new_commits(tmp_path):
    a, b, sa, sb, context = _accounts_and_initial_steps()
    accounts, steps = (a, b), (sa, sb)
    with PaperStore(tmp_path / "db") as store:
        store.ensure_schema()
        assert hasattr(store, "initialize_portfolio_with_receipt")
        committed = store.initialize_portfolio_with_receipt(
            accounts, steps, context, batch_id="first"
        )
        rows = (
            store._require_connection()
            .execute(
                "SELECT run_id, batch_id, account_id, trade_date, dataset_id, "
                "curated_sha256, state_hash "
                "FROM paper_runs ORDER BY trade_date, account_id"
            )
            .fetchall()
        )
        assert [tuple(asdict(r).values()) for r in committed.run_refs] == rows
        assert committed.states == store.audit_portfolio(accounts[0].portfolio_id)
        with pytest.raises(PaperIdempotencyError):
            store.commit_batch_with_receipt(accounts, steps, context, batch_id="duplicate")
        assert store._require_connection().execute(
            "SELECT COUNT(*) FROM paper_runs"
        ).fetchone() == (2,)


@pytest.mark.parametrize("failure", ["read", "second_account"])
def test_receipt_failure_and_second_account_failure_roll_back(tmp_path, monkeypatch, failure):
    a, b, sa, sb, context = _accounts_and_initial_steps()
    accounts, steps = (a, b), (sa, sb)
    with PaperStore(tmp_path / "db") as store:
        store.ensure_schema()
        assert hasattr(store, "initialize_portfolio_with_receipt")

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic failure")

        if failure == "read":
            monkeypatch.setattr(store, "_read_batch_references", fail)
        else:
            original = store._append_step

            def append(account, *args):
                if account == accounts[1]:
                    fail()
                return original(account, *args)

            monkeypatch.setattr(store, "_append_step", append)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            store.initialize_portfolio_with_receipt(accounts, steps, context, batch_id="first")
        for table in ("paper_accounts", "paper_runs", "paper_events", "paper_state"):
            assert store._require_connection().execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone() == (0,)


def test_real_pipeline_identity_attempts_and_noop(tmp_path):
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    day = prices.trade_date.max().date()
    first = automation(tmp_path, day, paper_run_portfolio)
    assert first.payload["schema_version"] == 2
    refs = first.payload["run_identity"]["ledger"]
    assert refs["scope"] == "complete" and len(refs["runs"]) == 2
    assert inspect(tmp_path)["automation_ledger_correlation"] == "verified"
    repeated = automation(tmp_path, day, paper_run_portfolio)
    assert first.payload["attempt_id"] != repeated.payload["attempt_id"]
    assert repeated.payload["processed_dates"] == []
    assert repeated.payload["run_identity"]["ledger"] == {"scope": "complete", "runs": []}
    assert inspect(tmp_path)["automation_ledger_correlation"] == "no_new_commits"
    identity = first.payload["run_identity"]["report"]
    assert identity is not None and "/" not in identity["archive_json_name"]
    assert (
        hashlib.sha256((paths.outputs / identity["archive_json_name"]).read_bytes()).hexdigest()
        == identity["archive_json_sha256"]
    )


def test_partial_failure_keeps_only_returned_commits(tmp_path, monkeypatch):
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    paper_init_portfolio(root=tmp_path, as_of_date=prices.trade_date.max().date())
    updated, day = _append_rising_bars(paths, prices, 3)
    assert hasattr(PaperStore, "commit_batch_with_receipt")
    original = PaperStore.commit_batch_with_receipt
    calls = 0

    def commit(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("later date failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PaperStore, "commit_batch_with_receipt", commit)
    result = automation(tmp_path, day, paper_run_portfolio)
    assert result.payload["status"] == "failed"
    assert result.payload["operation_status"] == "partial"
    assert result.payload["failed_step"] == "paper_run"
    assert result.payload["processed_dates"] == [updated.trade_date.iloc[-3].date().isoformat()]
    assert result.payload["ledger_committed"] is True
    assert len(result.payload["run_identity"]["ledger"]["runs"]) == 2
    assert inspect(tmp_path)["automation_ledger_correlation"] == "partial_verified"


def test_report_failure_and_archive_failure_keep_receipts(tmp_path, monkeypatch):
    import finance_lab.paper_automation as writer
    import finance_lab.paper_pipeline as pipeline

    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    day = prices.trade_date.max().date()

    def fail(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(pipeline, "write_paper_report", fail)
    result = automation(tmp_path, day, paper_run_portfolio)
    assert "run_identity" in result.payload
    assert result.payload["failed_step"] == "paper_report"
    assert result.payload["run_identity"]["report"] is None
    assert len(result.payload["run_identity"]["ledger"]["runs"]) == 2
    monkeypatch.setattr(writer, "_write_immutable_json", fail)
    with pytest.raises(writer.PaperAutomationStatusWriteError) as error:
        automation(tmp_path, day, paper_run_portfolio)
    assert len(error.value.attempt_id) == 32
    assert error.value.archive_confirmed is False
    assert error.value.ledger_runs == ()


def test_legacy_constructor_remains_unknown(tmp_path):
    operation = PaperOperationResult("no-op", "default", (), (), None, tmp_path / "report.html")
    result = automation(tmp_path, date(2026, 9, 8), lambda **kw: operation)
    assert result.payload.get("run_identity", {}).get("ledger") == {"scope": "unknown", "runs": []}
    assert inspect(tmp_path)["automation_ledger_correlation"] == "not_available"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("state_hash", "mismatch"),
        ("missing_account", "mismatch"),
        ("duplicate", "not_available"),
        ("bad_date", "not_available"),
        ("wal", "unavailable"),
        ("changed", "unavailable"),
    ],
)
def test_precise_reference_validation(tmp_path, monkeypatch, mutation, expected):
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    day = prices.trade_date.max().date()
    result = automation(tmp_path, day, paper_run_portfolio)
    assert "run_identity" in result.payload
    value = result.payload
    runs = value["run_identity"]["ledger"]["runs"]
    if mutation == "state_hash":
        runs[0]["state_hash"] = "f" * 64
    elif mutation == "missing_account":
        runs.pop()
    elif mutation == "duplicate":
        runs.append(dict(runs[0]))
    elif mutation == "bad_date":
        runs[0]["trade_date"] = "20260908"
    elif mutation == "wal":
        paths.database.with_name(paths.database.name + ".wal").write_bytes(b"synthetic")
    elif mutation == "changed":
        from finance_lab.operational_status_io import Observation

        monkeypatch.setattr(Observation, "changed", lambda self: {"automation"})
    if mutation not in {"wal", "changed"}:
        from tests.test_paper_automation_history import seal

        value = seal(value)
        result.archive_path.unlink()
        stamp = datetime.fromisoformat(value["started_at"].replace("Z", "+00:00"))
        target = (
            result.archive_path.parent
            / f"default_{stamp.strftime('%Y%m%dT%H%M%S%fZ')}_{value['run_id']}.json"
        )
        target.write_text(json.dumps(value))
        result.latest_path.write_text(json.dumps(value))
    assert inspect(tmp_path)["automation_ledger_correlation"] == expected


def test_historical_reference_not_latest_and_invalid_newest_no_fallback(tmp_path):
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    day = prices.trade_date.max().date()
    first = automation(tmp_path, day, paper_run_portfolio)
    _, later = _append_rising_bars(paths, prices, 1)
    paper_run_portfolio(root=tmp_path, as_of_date=later)
    assert inspect(tmp_path)["automation_ledger_correlation"] == "verified"
    (first.archive_path.parent / "default_20990101T000000000000Z_aaaaaaaaaaaaaaaa.json").write_text(
        "{}"
    )
    assert inspect(tmp_path)["automation_ledger_correlation"] == "not_available"


def test_schema1_has_no_correlation(tmp_path):
    archive(tmp_path, payload())
    assert inspect(tmp_path, "study-1")["automation_ledger_correlation"] == "not_available"


@pytest.mark.parametrize("metadata", ["report", "health", "receipt"])
def test_bad_injected_metadata_archives_failure_not_unhandled_error(tmp_path, metadata):
    import finance_lab.paper_models as models

    assert hasattr(models, "PaperRunReference")
    operation = PaperOperationResult("no-op", "default", (), (), None, tmp_path / "report.html", ())
    health = {"health_status": "pass"}
    if metadata == "report":
        operation = replace(
            operation,
            report_identity={
                "archive_json_name": "../private.json",
                "archive_json_sha256": "a" * 64,
            },
        )
    elif metadata == "health":
        health.update(dataset_id="opaque", as_of_date="2026-09-07")
    else:
        operation = replace(
            operation,
            ledger_runs=(
                models.PaperRunReference(
                    "paper-run-" + "a" * 24,
                    "batch",
                    "account",
                    date(2026, 9, 8),
                    "opaque",
                    "b" * 64,
                    "c" * 64,
                ),
            ),
        )
    actions = PaperAutomationActions(
        lambda **kw: {"status": "success", "successful_instruments": 1, "failed_sources": 0},
        lambda **kw: (tmp_path / "health.html", health),
        lambda **kw: operation,
        paper_init_portfolio,
    )
    result = run_paper_daily_automation(root=tmp_path, as_of_date=date(2026, 9, 8), actions=actions)
    assert result.payload["status"] == "failed"
    assert result.payload["run_identity"]["ledger"] == {"scope": "unknown", "runs": []}


def test_store_exit_failure_after_daily_commit_is_partial(tmp_path, monkeypatch):
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    paper_init_portfolio(root=tmp_path, as_of_date=prices.trade_date.max().date())
    _, day = _append_rising_bars(paths, prices, 1)
    assert hasattr(PaperStore, "commit_batch_with_receipt")
    original = PaperStore.__exit__

    def close(self, *args):
        original(self, *args)
        if not self.read_only:
            raise RuntimeError("close failed after receipt returned")

    monkeypatch.setattr(PaperStore, "__exit__", close)
    result = automation(tmp_path, day, paper_run_portfolio)
    assert result.payload["operation_status"] == "partial"
    assert len(result.payload["run_identity"]["ledger"]["runs"]) == 2


def test_real_market_receipt_is_completed_locked_batch(tmp_path, monkeypatch):
    from tests.test_market_data_state import _install_fetch, _update

    _, prices = make_paper_test_project(tmp_path)
    _install_fetch(monkeypatch, prices)
    result = _update(tmp_path, prices)
    assert "market_identity" in result
    marker = json.loads((tmp_path / "data" / "market_update_state.json").read_text())
    assert marker["status"] == "completed"
    assert result["market_identity"] == {key: marker[key] for key in ("batch_id", "summary_sha256")}


def test_legacy_opaque_input_identity_survives_receipt_api(tmp_path):
    a, b, sa, sb, context = _accounts_and_initial_steps()
    context = replace(
        context, dataset_id="legacy dataset v1", curated_sha256="legacy-curated-input"
    )
    with PaperStore(tmp_path / "db") as store:
        store.ensure_schema()
        result = store.initialize_portfolio_with_receipt(
            (a, b), (sa, sb), context, batch_id="legacy"
        )
        assert all(r.curated_sha256 == "legacy-curated-input" for r in result.run_refs)


def test_old_store_api_does_not_apply_new_archive_character_policy(tmp_path):
    a, b, sa, sb, context = _accounts_and_initial_steps()
    context = replace(context, dataset_id="", curated_sha256="")
    with PaperStore(tmp_path / "db") as store:
        store.ensure_schema()
        states = store.initialize_portfolio((a, b), (sa, sb), context, batch_id="")
        assert type(states) is tuple and len(states) == 2


@pytest.mark.parametrize("error_kind", ["report", "partial"])
def test_malformed_typed_error_does_not_prevent_failure_archive(tmp_path, error_kind):
    from finance_lab.paper_models import PaperRunReference
    from finance_lab.paper_pipeline import PaperPartialOperationError, PaperReportWriteError

    ref = PaperRunReference(
        "paper-run-" + "a" * 24, "batch", "account", date(2026, 9, 8), "opaque", "c" * 64, "d" * 64
    )
    if error_kind == "report":
        operation = PaperOperationResult("no-op", "default", (), (), None, None, (ref,))
        error = PaperReportWriteError(operation)
    else:
        error = PaperPartialOperationError("default", (), (ref,))

    def run(**kw):
        raise error

    result = automation(tmp_path, date(2026, 9, 8), run)
    assert result.payload["status"] == "failed"
    assert result.payload["run_identity"]["ledger"] == {"scope": "unknown", "runs": []}


def test_schema2_missing_inherited_field_is_protocol_invalid(tmp_path):
    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    result = automation(tmp_path, prices.trade_date.max().date(), paper_run_portfolio)
    from finance_lab.paper_automation_history import _validate
    from tests.test_paper_automation_history import seal

    value = dict(result.payload)
    value.pop("ledger_committed")
    with pytest.raises(ValueError):
        _validate(seal(value), "default")


@pytest.mark.parametrize(
    "scope,expected",
    [
        ("complete", "mismatch"),
        ("partial", "mismatch"),
        ("no-op", "unavailable"),
        ("unknown", "not_available"),
    ],
)
def test_audited_missing_whole_portfolio_uses_explicit_receipt_scope(tmp_path, scope, expected):
    import duckdb

    from tests.test_paper_automation_history import seal

    paths, prices = make_paper_test_project(tmp_path, rising_prices=True)
    result = automation(tmp_path, prices.trade_date.max().date(), paper_run_portfolio)
    value = result.payload
    if scope == "partial":
        value.update(
            status="failed",
            operation_status="partial",
            failed_step="paper_run",
            error={
                "type": "PaperPartialOperationError",
                "message": "synthetic failure",
                "code": "paper_run_failed",
            },
            paper_report_path=None,
        )
        value["steps"] = value["steps"][:2] + [{"name": "paper_run", "status": "failed"}]
        value["run_identity"]["ledger"]["scope"] = "partial"
        value["run_identity"]["report"] = None
    elif scope == "no-op":
        value.update(operation_status="no-op", processed_dates=[], ledger_committed=False)
        value["steps"] = value["steps"][:2] + [{"name": "paper_run", "status": "succeeded"}]
        value["run_identity"]["ledger"] = {"scope": "complete", "runs": []}
    elif scope == "unknown":
        value["run_identity"]["ledger"] = {"scope": "unknown", "runs": []}
    value = seal(value)
    result.archive_path.unlink()
    stamp = datetime.fromisoformat(value["started_at"].replace("Z", "+00:00"))
    name = f"default_{stamp.strftime('%Y%m%dT%H%M%S%fZ')}_{value['run_id']}.json"
    (result.archive_path.parent / name).write_text(json.dumps(value))
    result.latest_path.write_text(json.dumps(value))
    with duckdb.connect(str(paths.database)) as connection:
        for table in ("paper_events", "paper_state", "paper_runs", "paper_accounts"):
            connection.execute(f"DELETE FROM {table}")
    status = inspect(tmp_path)
    assert status["ledger"]["audit_status"] == "passed"
    assert status["automation_ledger_correlation"] == expected
    assert status["ledger"]["correlation"]["status"] == expected
