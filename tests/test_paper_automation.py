from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

import finance_lab.cli as cli_module
import finance_lab.paper_automation as paper_automation_module
from finance_lab.cli import build_parser, main
from finance_lab.paper_automation import (
    PaperAutomationActions,
    PaperAutomationResult,
    PaperAutomationStatusWriteError,
    run_paper_daily_automation,
)
from finance_lab.paper_models import PaperOperationResult
from finance_lab.paper_pipeline import PaperPortfolioNotFound, PaperReportWriteError


def _operation(
    status: str = "processed",
    *,
    report_path: Path | None = Path("paper-report.html"),
) -> PaperOperationResult:
    return PaperOperationResult(
        status=status,  # type: ignore[arg-type]
        portfolio_id="study-1",
        processed_dates=(date(2026, 9, 8),),
        states=(),
        data_context=None,
        report_path=report_path,
    )


def _clock(*values: datetime):
    moments = iter(values)
    return lambda: next(moments)


def test_daily_automation_records_successful_steps_and_outputs(tmp_path: Path) -> None:
    calls: list[str] = []

    def update(**_kwargs: object) -> dict[str, object]:
        calls.append("update")
        return {"successful_instruments": 2, "failed_sources": 0, "status": "success"}

    def health(**_kwargs: object) -> tuple[Path, dict[str, object]]:
        calls.append("health")
        return tmp_path / "health.html", {"health_status": "warning"}

    def run(**_kwargs: object) -> PaperOperationResult:
        calls.append("run")
        return _operation(report_path=tmp_path / "outputs" / "study-report.html")

    def unexpected_init(**_kwargs: object) -> PaperOperationResult:
        raise AssertionError("existing portfolio must not be initialized")

    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        stale_after_business_days=3,
        root=tmp_path,
        actions=PaperAutomationActions(update, health, run, unexpected_init),
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert calls == ["update", "health", "run"]
    assert result.succeeded
    assert result.exit_code == 0
    assert result.archive_path.exists()
    assert result.latest_path.exists()
    assert result.archive_path != result.latest_path
    assert result.archive_path.resolve().is_relative_to(
        (tmp_path / "outputs").resolve()
    )
    archive = json.loads(result.archive_path.read_text(encoding="utf-8"))
    latest = json.loads(result.latest_path.read_text(encoding="utf-8"))
    assert latest == archive == result.payload
    assert archive["schema_version"] == 1
    assert archive["workflow"] == "paper_daily"
    assert archive["status"] == "success"
    assert archive["portfolio_id"] == "study-1"
    assert archive["as_of_date"] == "2026-09-08"
    assert archive["operation_status"] == "processed"
    assert archive["processed_dates"] == ["2026-09-08"]
    assert archive["data_health_status"] == "warning"
    assert archive["failed_sources"] == 0
    assert archive["market_data_committed"] is True
    assert archive["failed_step"] is None
    assert archive["error"] is None
    assert archive["broker_connected"] is False
    assert archive["authorizes_real_money"] is False
    assert archive["steps"] == [
        {"name": "market_data_update", "status": "succeeded"},
        {"name": "data_health", "status": "succeeded"},
        {"name": "paper_run", "status": "succeeded"},
    ]


def test_daily_automation_initializes_only_when_portfolio_is_missing(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def missing(**_kwargs: object) -> PaperOperationResult:
        calls.append("run")
        raise PaperPortfolioNotFound("missing")

    def initialize(**_kwargs: object) -> PaperOperationResult:
        calls.append("init")
        return _operation("initialized")

    actions = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        missing,
        initialize,
    )

    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert calls == ["run", "init"]
    assert result.succeeded
    assert result.payload["operation_status"] == "initialized"
    assert result.payload["steps"][-2:] == [
        {"name": "paper_run", "status": "portfolio_missing"},
        {"name": "paper_init", "status": "succeeded"},
    ]


@pytest.mark.parametrize(
    ("step", "status", "processed_dates"),
    [
        ("paper_run", "status", ()),
        ("paper_run", "initialized", (date(2026, 9, 8),)),
        ("paper_run", "processed", ()),
        ("paper_run", "no-op", (date(2026, 9, 8),)),
        ("paper_run", "processed", (date(2026, 9, 9),)),
        ("paper_run", "processed", (date(2026, 9, 8), date(2026, 9, 8))),
        ("paper_init", "processed", (date(2026, 9, 8),)),
        ("paper_init", "initialized", ()),
    ],
)
def test_daily_automation_rejects_inconsistent_operation_result(
    tmp_path: Path,
    step: str,
    status: str,
    processed_dates: tuple[date, ...],
) -> None:
    def invalid(**_kwargs: object) -> PaperOperationResult:
        return PaperOperationResult(
            status=status,  # type: ignore[arg-type]
            portfolio_id="study-1",
            processed_dates=processed_dates,
            states=(),
            data_context=None,
        )

    def missing(**_kwargs: object) -> PaperOperationResult:
        raise PaperPortfolioNotFound("missing")

    actions = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        invalid if step == "paper_run" else missing,
        invalid if step == "paper_init" else lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("init must not run")
        ),
    )
    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert result.exit_code == 1
    assert result.payload["status"] == "failed"
    assert result.payload["failed_step"] == step
    assert result.payload["ledger_committed"] is None
    assert result.payload["operation_status"] is None
    assert result.payload["processed_dates"] == []
    assert json.loads(result.archive_path.read_text(encoding="utf-8")) == result.payload


def test_daily_automation_accepts_idempotent_no_op(tmp_path: Path) -> None:
    actions = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        lambda **_kwargs: PaperOperationResult(
            status="no-op",
            portfolio_id="study-1",
            processed_dates=(),
            states=(),
            data_context=None,
            report_path=tmp_path / "paper-report.html",
        ),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("init must not run")),
    )

    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert result.succeeded
    assert result.payload["operation_status"] == "no-op"
    assert result.payload["processed_dates"] == []
    assert result.payload["ledger_committed"] is False


def test_daily_automation_rejects_health_without_report_path(tmp_path: Path) -> None:
    actions = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (None, {"health_status": "pass"}),  # type: ignore[return-value]
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("paper must not run")),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("init must not run")),
    )
    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert result.exit_code == 1
    assert result.payload["failed_step"] == "data_health"
    assert result.payload["data_health_report_path"] is None
    assert json.loads(result.archive_path.read_text(encoding="utf-8")) == result.payload


@pytest.mark.parametrize(
    ("step", "status"),
    [("paper_run", "processed"), ("paper_run", "no-op"), ("paper_init", "initialized")],
)
def test_daily_automation_rejects_success_without_report_path(
    tmp_path: Path,
    step: str,
    status: str,
) -> None:
    def no_report(**_kwargs: object) -> PaperOperationResult:
        return PaperOperationResult(
            status=status,  # type: ignore[arg-type]
            portfolio_id="study-1",
            processed_dates=(date(2026, 9, 8),) if status != "no-op" else (),
            states=(),
            data_context=None,
            report_path=None,
        )

    actions = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        no_report if step == "paper_run" else lambda **_kwargs: (_ for _ in ()).throw(
            PaperPortfolioNotFound("missing")
        ),
        no_report if step == "paper_init" else lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("init must not run")
        ),
    )
    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert result.exit_code == 1
    assert result.payload["failed_step"] == step
    assert result.payload["ledger_committed"] is None
    assert result.payload["paper_report_path"] is None


def test_daily_automation_records_ledger_commit_when_report_fails(tmp_path: Path) -> None:
    def fail_report(**_kwargs: object) -> PaperOperationResult:
        raise PaperReportWriteError(_operation("processed"))

    actions = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        fail_report,
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("init must not run")),
    )
    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert result.exit_code == 1
    assert result.payload["status"] == "failed"
    assert result.payload["failed_step"] == "paper_report"
    assert result.payload["market_data_committed"] is True
    assert result.payload["ledger_committed"] is True
    assert result.payload["operation_status"] == "processed"
    assert result.payload["processed_dates"] == ["2026-09-08"]
    assert "paper-status" in str(result.payload["next_action"])
    assert result.payload["steps"][-2:] == [
        {"name": "paper_run", "status": "succeeded"},
        {"name": "paper_report", "status": "failed"},
    ]
    assert json.loads(result.latest_path.read_text(encoding="utf-8")) == result.payload


@pytest.mark.parametrize(
    ("failure_stage", "writer_name", "archive_confirmed"),
    [
        ("write_status_archive", "_write_immutable_json", False),
        ("write_latest_status", "_write_json_atomic", True),
    ],
)
def test_daily_automation_status_write_failure_preserves_commit_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    writer_name: str,
    archive_confirmed: bool,
) -> None:
    actions = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        lambda **_kwargs: _operation("processed"),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("init must not run")),
    )
    latest = tmp_path / "outputs" / "study-1_paper_automation_latest.json"
    latest.parent.mkdir()
    latest.write_text("previous status", encoding="utf-8")

    def fail_write(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("private disk path must not be printed")

    monkeypatch.setattr(paper_automation_module, writer_name, fail_write)
    with pytest.raises(PaperAutomationStatusWriteError) as raised:
        run_paper_daily_automation(
            portfolio_id="study-1",
            start_date=date(2018, 1, 1),
            as_of_date=date(2026, 9, 8),
            root=tmp_path,
            actions=actions,
            clock=_clock(
                datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
                datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
            ),
        )

    failure = raised.value
    assert failure.stage == failure_stage
    assert failure.market_data_committed is True
    assert failure.ledger_committed is True
    assert failure.processed_dates == ("2026-09-08",)
    assert failure.archive_confirmed is archive_confirmed
    assert (failure.archive_path is not None) is archive_confirmed
    if failure.archive_path is not None:
        assert failure.archive_path.is_file()
    assert latest.read_text(encoding="utf-8") == "previous status"
    assert "private disk path" not in str(failure)


def test_status_write_failure_keeps_interrupted_update_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted_update(**_kwargs: object) -> dict[str, object]:
        raise duckdb.IOException("private token=C:/secret/database")

    def forbidden(**_kwargs: object) -> object:
        raise AssertionError("downstream step must not start")

    def fail_archive(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("private archive disk path")

    monkeypatch.setattr(paper_automation_module, "_write_immutable_json", fail_archive)
    actions = PaperAutomationActions(interrupted_update, forbidden, forbidden, forbidden)
    with pytest.raises(PaperAutomationStatusWriteError) as raised:
        run_paper_daily_automation(
            portfolio_id="study-1",
            start_date=date(2018, 1, 1),
            as_of_date=date(2026, 9, 8),
            root=tmp_path,
            actions=actions,
            clock=_clock(
                datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
                datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
            ),
        )
    failure = raised.value
    assert failure.stage == "write_status_archive"
    assert failure.market_data_committed is None
    assert failure.ledger_committed is None
    assert failure.archive_confirmed is False
    assert failure.archive_path is None
    assert "private" not in str(failure)


def test_daily_automation_rejects_partial_source_update(tmp_path: Path) -> None:
    actions = PaperAutomationActions(
        lambda **_kwargs: {
            "successful_instruments": 1,
            "failed_sources": 1,
            "status": "partial_sources",
        },
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("health must not run")),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("paper must not run")),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("init must not run")),
    )
    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )
    assert not result.succeeded
    assert result.exit_code != 0
    assert result.payload["failed_step"] == "market_data_update"
    assert result.payload["failed_sources"] == 1
    assert result.payload["market_data_committed"] is True
    assert "data-update-status" in str(result.payload["next_action"])


@pytest.mark.parametrize(
    ("update_payload", "committed"),
    [
        ({"successful_instruments": 1, "failed_sources": 0}, None),
        ({"successful_instruments": 1, "failed_sources": 0, "status": "unknown"}, None),
        ({"successful_instruments": 1.0, "status": "success"}, None),
        ({"successful_instruments": True, "status": "success"}, None),
        ({"successful_instruments": 0, "failed_sources": 0, "status": "success"}, False),
        ({"successful_instruments": 1, "status": "success"}, True),
        ({"successful_instruments": 1, "failed_sources": 1, "status": "success"}, True),
    ],
)
def test_daily_automation_requires_confirmed_complete_update(
    tmp_path: Path,
    update_payload: dict[str, object],
    committed: bool | None,
) -> None:
    actions = PaperAutomationActions(
        lambda **_kwargs: update_payload,
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("health must not run")),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("paper must not run")),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("init must not run")),
    )
    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )
    assert result.exit_code == 1
    assert result.payload["failed_step"] == "market_data_update"
    assert result.payload["market_data_committed"] is committed
    assert result.payload["ledger_committed"] is None
    assert "data-update-status" in str(result.payload["next_action"])


def test_daily_automation_records_failure_step_and_nonzero_exit(
    tmp_path: Path,
) -> None:
    def offline(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("data source offline")

    actions = PaperAutomationActions(
        offline,
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("health must not run")),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("paper must not run")),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("init must not run")),
    )

    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=actions,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert not result.succeeded
    assert result.exit_code == 1
    assert result.payload["status"] == "failed"
    assert result.payload["failed_step"] == "market_data_update"
    assert result.payload["market_data_committed"] is None
    assert result.payload["steps"] == [
        {"name": "market_data_update", "status": "failed"}
    ]
    assert result.payload["error"] == {
        "type": "RuntimeError",
        "code": "market_data_update_failed",
        "message": "行情更新未完成或数据源未全部成功；不能推进模拟盘。",
    }
    assert json.loads(result.latest_path.read_text(encoding="utf-8")) == result.payload


@pytest.mark.parametrize(
    ("failure_step", "expected_calls", "committed"),
    [
        ("market_data_update", ["update"], None),
        ("data_health", ["update", "health"], True),
        ("paper_run", ["update", "health", "run"], True),
    ],
)
def test_daily_automation_redacts_private_errors_and_preserves_update_state(
    tmp_path: Path,
    failure_step: str,
    expected_calls: list[str],
    committed: bool | None,
) -> None:
    calls: list[str] = []

    def update(**_kwargs: object) -> dict[str, object]:
        calls.append("update")
        if failure_step == "market_data_update":
            raise OSError("private token=C:/secret/update")
        return {"successful_instruments": 1, "failed_sources": 0, "status": "success"}

    def health(**_kwargs: object) -> tuple[Path, dict[str, object]]:
        calls.append("health")
        if failure_step == "data_health":
            raise OSError("private token=C:/secret/health")
        return tmp_path / "health.html", {"health_status": "pass"}

    def run(**_kwargs: object) -> PaperOperationResult:
        calls.append("run")
        raise OSError("private token=C:/secret/ledger")

    def unexpected_init(**_kwargs: object) -> PaperOperationResult:
        raise AssertionError("initialization must not follow this failure")

    result = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=PaperAutomationActions(update, health, run, unexpected_init),
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )

    assert calls == expected_calls
    assert result.exit_code == 1
    assert result.payload["failed_step"] == failure_step
    assert result.payload["market_data_committed"] is committed
    assert result.payload["ledger_committed"] is None
    assert result.payload["error"] == {
        "type": "OSError",
        "code": f"{failure_step}_failed",
        "message": paper_automation_module._failure_message(failure_step),
    }
    assert "private token" not in json.dumps(result.payload)
    assert "private token" not in result.archive_path.read_text(encoding="utf-8")
    assert "private token" not in result.latest_path.read_text(encoding="utf-8")
    assert json.loads(result.archive_path.read_text(encoding="utf-8")) == result.payload


def test_paper_daily_cli_redacts_interrupted_database_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted_update(**_kwargs: object) -> dict[str, object]:
        raise duckdb.IOException("private token=C:/secret/database")

    def forbidden(**_kwargs: object) -> object:
        raise AssertionError("downstream step must not start")

    actions = PaperAutomationActions(interrupted_update, forbidden, forbidden, forbidden)
    monkeypatch.setattr(paper_automation_module, "_default_actions", lambda: actions)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert main(["paper-daily", "--portfolio", "study-1", "--as-of", "2026-09-08"]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "market_data_update"
    assert payload["error"] == {
        "type": "IOException",
        "code": "market_data_update_failed",
        "message": "行情更新未完成或数据源未全部成功；不能推进模拟盘。",
    }
    assert payload["market_data_committed"] is None
    assert payload["ledger_committed"] is None
    assert "data-update-status" in str(payload["next_action"])
    assert "private token" not in output.out + output.err
    latest = tmp_path / "outputs" / "study-1_paper_automation_latest.json"
    assert json.loads(latest.read_text(encoding="utf-8")) == payload
    archive_paths = list((tmp_path / "outputs" / "paper_automation_runs").glob("*.json"))
    assert len(archive_paths) == 1
    assert json.loads(archive_paths[0].read_text(encoding="utf-8")) == payload


def test_daily_automation_preserves_old_archive_when_latest_status_changes(
    tmp_path: Path,
) -> None:
    failing = PaperAutomationActions(
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        lambda **_kwargs: _operation(),
        lambda **_kwargs: _operation("initialized"),
    )
    first = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 8),
        root=tmp_path,
        actions=failing,
        clock=_clock(
            datetime(2026, 9, 8, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 8, 10, 31, tzinfo=UTC),
        ),
    )
    first_bytes = first.archive_path.read_bytes()
    succeeding = PaperAutomationActions(
        lambda **_kwargs: {"successful_instruments": 1, "failed_sources": 0, "status": "success"},
        lambda **_kwargs: (tmp_path / "health.html", {"health_status": "pass"}),
        lambda **_kwargs: _operation(),
        lambda **_kwargs: _operation("initialized"),
    )

    second = run_paper_daily_automation(
        portfolio_id="study-1",
        start_date=date(2018, 1, 1),
        as_of_date=date(2026, 9, 9),
        root=tmp_path,
        actions=succeeding,
        clock=_clock(
            datetime(2026, 9, 9, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 9, 10, 31, tzinfo=UTC),
        ),
    )

    assert first.archive_path != second.archive_path
    assert first.archive_path.read_bytes() == first_bytes
    assert json.loads(second.latest_path.read_text(encoding="utf-8"))["status"] == "success"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"portfolio_id": "../escape"},
        {"start_date": date(2026, 9, 8), "as_of_date": date(2026, 9, 8)},
        {"stale_after_business_days": -1},
    ],
)
def test_daily_automation_rejects_invalid_invocation(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    parameters: dict[str, object] = {
        "portfolio_id": "study-1",
        "start_date": date(2018, 1, 1),
        "as_of_date": date(2026, 9, 8),
        "stale_after_business_days": 3,
        "root": tmp_path,
    }
    parameters.update(kwargs)

    with pytest.raises(ValueError):
        run_paper_daily_automation(**parameters)  # type: ignore[arg-type]


def test_paper_daily_cli_dispatches_and_returns_recorded_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    received: dict[str, object] = {}
    payload = {"status": "failed", "failed_step": "data_health"}
    expected = PaperAutomationResult(
        payload=payload,
        archive_path=tmp_path / "archive.json",
        latest_path=tmp_path / "latest.json",
    )

    def fake_daily(**kwargs: object) -> PaperAutomationResult:
        received.update(kwargs)
        return expected

    monkeypatch.setattr(cli_module, "run_paper_daily_automation", fake_daily)

    exit_code = main(
        [
            "paper-daily",
            "--portfolio",
            "study-1",
            "--start",
            "2018-01-01",
            "--as-of",
            "2026-09-08",
            "--stale-after-business-days",
            "2",
        ]
    )

    assert exit_code == 1
    assert received == {
        "portfolio_id": "study-1",
        "start_date": date(2018, 1, 1),
        "as_of_date": date(2026, 9, 8),
        "stale_after_business_days": 2,
    }
    assert "data_health" in capsys.readouterr().err


def test_paper_daily_cli_status_write_failure_is_single_safe_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    archive = tmp_path / "outputs" / "archive.json"
    failure = PaperAutomationStatusWriteError(
        portfolio_id="study-1",
        stage="write_latest_status",
        market_data_committed=True,
        ledger_committed=True,
        processed_dates=["2026-09-08"],
        archive_confirmed=True,
        archive_path=archive,
    )

    def fail_daily(**_kwargs: object) -> PaperAutomationResult:
        raise failure from OSError("private disk diagnostic")

    monkeypatch.setattr(cli_module, "run_paper_daily_automation", fail_daily)
    assert main(["paper-daily", "--portfolio", "study-1"]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["error_code"] == "paper_automation_status_write_failed"
    assert payload["stage"] == "write_latest_status"
    assert payload["market_data_committed"] is True
    assert payload["ledger_committed"] is True
    assert payload["status_archive_confirmed"] is True
    assert payload["status_archive_path"] == str(archive)
    assert payload["latest_status_state"] == "unknown"
    assert "paper-status" in payload["next_action"]
    assert "private disk diagnostic" not in output.out + output.err
    assert output.err == ""


def test_paper_daily_cli_parser_has_documented_defaults() -> None:
    args = build_parser().parse_args(["paper-daily"])

    assert args.portfolio == "default"
    assert args.start == date(2018, 1, 1)
    assert args.stale_after_business_days == 3


def test_windows_daily_scripts_are_reversible_and_reject_worktrees() -> None:
    root = Path(__file__).resolve().parents[1]
    runner = (root / "scripts" / "run_paper_daily.ps1").read_text(encoding="utf-8")
    installer = (root / "scripts" / "install_paper_daily_task.ps1").read_text(
        encoding="utf-8"
    )

    assert "finance_lab.cli paper-daily" in runner
    assert "NotifyIcon" in runner
    assert "$LASTEXITCODE" in runner
    assert "paper_automation_logs" in runner
    assert "Start-Process" not in runner
    assert "Register-ScheduledTask" in installer
    assert "Unregister-ScheduledTask" in installer
    assert ".worktrees" in installer
    assert "Monday" in installer and "Friday" in installer
    assert "Interactive" in installer
    assert "Limited" in installer


def test_release_package_includes_daily_automation_entry_points() -> None:
    root = Path(__file__).resolve().parents[1]
    package_script = (root / "scripts" / "package_release.ps1").read_text(
        encoding="utf-8"
    )

    for required_path in (
        "src\\finance_lab\\paper_automation.py",
        "tests\\test_paper_automation.py",
        "scripts\\run_paper_daily.ps1",
        "scripts\\install_paper_daily_task.ps1",
        "run_paper_daily.cmd",
        "install_paper_daily_task.cmd",
        "uninstall_paper_daily_task.cmd",
    ):
        assert required_path in package_script


def test_windows_daily_scripts_have_valid_powershell_syntax() -> None:
    root = Path(__file__).resolve().parents[1]
    for filename in ("run_paper_daily.ps1", "install_paper_daily_task.ps1"):
        script_path = root / "scripts" / filename
        command = (
            "$tokens = $null; $errors = $null; "
            "$null = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{script_path}', [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -ne 0) { "
            "$errors | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
