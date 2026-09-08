from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import finance_lab.cli as cli_module
from finance_lab.cli import build_parser, main
from finance_lab.paper_automation import (
    PaperAutomationActions,
    PaperAutomationResult,
    run_paper_daily_automation,
)
from finance_lab.paper_models import PaperOperationResult
from finance_lab.paper_pipeline import PaperPortfolioNotFound


def _operation(
    status: str = "processed",
    *,
    report_path: Path | None = None,
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
        return {"successful_instruments": 2}

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
        lambda **_kwargs: {"successful_instruments": 1},
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
    assert result.payload["steps"] == [
        {"name": "market_data_update", "status": "failed"}
    ]
    assert result.payload["error"] == {
        "type": "RuntimeError",
        "message": "data source offline",
    }
    assert json.loads(result.latest_path.read_text(encoding="utf-8")) == result.payload


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
        lambda **_kwargs: {"successful_instruments": 1},
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
