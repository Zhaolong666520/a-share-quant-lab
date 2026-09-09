from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

import finance_lab.cli as cli_module
from finance_lab.cli import build_parser, main
from finance_lab.paper_models import (
    PaperOperationResult,
    PaperOperationStatus,
    PaperState,
    PendingOrder,
)
from finance_lab.paper_pipeline import PaperPortfolioNotFound


def _operation(
    status: PaperOperationStatus,
    report_path: Path | None = None,
) -> PaperOperationResult:
    return PaperOperationResult(
        status=status,
        portfolio_id="default",
        processed_dates=(),
        states=(),
        data_context=None,
        report_path=report_path,
    )


def test_paper_cli_parser_accepts_documented_arguments() -> None:
    parser = build_parser()

    initialized = parser.parse_args(
        [
            "paper-init",
            "--portfolio",
            "study-1",
            "--initial-cash",
            "200000",
            "--lot-size",
            "200",
            "--commission-bps",
            "4",
            "--minimum-commission",
            "6",
            "--sell-tax-bps",
            "1",
            "--slippage-bps",
            "3",
            "--as-of",
            "2026-08-28",
            "--stale-after-business-days",
            "2",
        ]
    )
    running = parser.parse_args(
        ["paper-run", "--portfolio", "study-1", "--as-of", "2026-08-28"]
    )
    status = parser.parse_args(["paper-status", "--portfolio", "study-1"])

    assert initialized.portfolio == "study-1"
    assert initialized.initial_cash == 200000.0
    assert initialized.lot_size == 200
    assert initialized.as_of.isoformat() == "2026-08-28"
    assert running.portfolio == "study-1"
    assert status.portfolio == "study-1"


def test_paper_cli_dispatches_init_and_prints_utf8_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: dict[str, object] = {}

    def fake_init(**kwargs: object) -> PaperOperationResult:
        received.update(kwargs)
        return _operation("initialized", Path("outputs/default_paper_latest_report.html"))

    monkeypatch.setattr(cli_module, "paper_init_portfolio", fake_init)

    exit_code = main(["paper-init", "--portfolio", "default", "--as-of", "2026-08-28"])

    assert exit_code == 0
    assert received["portfolio_id"] == "default"
    assert str(received["as_of_date"]) == "2026-08-28"
    assert "模拟盘报告已生成" in capsys.readouterr().out


def test_paper_cli_returns_four_for_missing_portfolio(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing(**_kwargs: object) -> PaperOperationResult:
        raise PaperPortfolioNotFound("账户组 default 不存在")

    monkeypatch.setattr(cli_module, "paper_status_portfolio", missing)

    assert main(["paper-status"]) == 4
    assert "不存在" in capsys.readouterr().err


def test_paper_cli_status_includes_pending_order_attempt_count() -> None:
    trade_date = date(2026, 8, 28)
    state = PaperState(
        account_id="default-momentum-120-v1",
        last_trade_date=trade_date,
        cash=1_000.0,
        shares=0,
        last_close=10.0,
        equity=1_000.0,
        equity_peak=1_000.0,
        drawdown=0.0,
        last_target_position=1,
        pending_order=PendingOrder("order-1", trade_date, "BUY", attempt_count=2),
        last_event_hash="h" * 64,
    )
    result = PaperOperationResult(
        status="status",
        portfolio_id="default",
        processed_dates=(),
        states=(state,),
        data_context=None,
    )

    payload = cli_module._paper_operation_payload(result)

    assert payload["accounts"][0]["pending_order"]["attempt_count"] == 2


def test_windows_paper_script_checks_every_command_and_uses_latest_report() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "run_paper_trading.ps1").read_text(encoding="utf-8")
    command = (root / "run_paper_trading.cmd").read_text(encoding="utf-8")

    assert "finance_lab.cli fetch" in script
    assert "finance_lab.cli manifest" in script
    assert "finance_lab.cli paper-run" in script
    assert "finance_lab.cli paper-init" in script
    assert "finance_lab.cli paper-status" in script
    assert script.count("$LASTEXITCODE") >= 5
    assert "_paper_latest_report.html" in script
    assert "run_paper_trading.ps1" in command


def test_windows_paper_script_has_valid_powershell_syntax() -> None:
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "run_paper_trading.ps1"
    command = (
        "$tokens = $null; $errors = $null; "
        "$null = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script_path}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -ne 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
