"""Batch report errors must describe confirmed progress without claiming rollback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab import cli

_BATCH_FUNCTIONS = [
    ("account-all", "account_backtest_symbol"),
    ("experiment-all", "experiment_symbol"),
    ("walk-forward-all", "walk_forward_symbol"),
    ("cost-stress-all", "cost_stress_symbol"),
    ("parameter-test-all", "parameter_test_symbol"),
    ("execution-test-all", "execution_test_symbol"),
    ("compare-all", "strategy_compare_symbol"),
]


def _prepare_config(root: Path) -> None:
    path = root / "config" / "instruments.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            [
                {"code": "510300", "exchange": "sh", "name": "A", "kind": "etf"},
                {"code": "510500", "exchange": "sh", "name": "B", "kind": "etf"},
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(("command", "function_name"), _BATCH_FUNCTIONS)
def test_batch_failure_reports_confirmed_partial_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    function_name: str,
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    _prepare_config(tmp_path)
    first_report = tmp_path / "outputs" / "first.html"

    def fake_report(symbol: str, **_kwargs: object) -> tuple[Path, dict[str, object]]:
        if symbol == "sh.510300":
            first_report.parent.mkdir(exist_ok=True)
            first_report.write_text("completed", encoding="utf-8")
            return first_report, {"status": "completed"}
        raise OSError("private write failure details")

    monkeypatch.setattr(cli, function_name, fake_report)
    assert cli.main([command]) != 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "batch_report_failed"
    assert payload["stage"] == "generate_reports"
    assert payload["data_committed"] is False
    assert payload["report_batch_complete"] is False
    assert payload["current_report_state"] == "unknown"
    assert payload["failed_symbol"] == "sh.510500"
    assert payload["reports_confirmed"] == [
        {"symbol": "sh.510300", "report": str(first_report)}
    ]
    assert payload["next_action"]
    assert "private write failure details" not in output.out
    assert output.err == ""
    assert first_report.read_text(encoding="utf-8") == "completed"


def test_batch_failure_does_not_claim_first_partial_file_was_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    _prepare_config(tmp_path)
    partial_report = tmp_path / "outputs" / "partial.html"

    def partial_write(_symbol: str, **_kwargs: object) -> tuple[Path, dict[str, object]]:
        partial_report.parent.mkdir(exist_ok=True)
        partial_report.write_text("incomplete", encoding="utf-8")
        raise OSError("private write failure details")

    monkeypatch.setattr(cli, "experiment_symbol", partial_write)
    assert cli.main(["experiment-all"]) != 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["reports_confirmed"] == []
    assert payload["failed_symbol"] == "sh.510300"
    assert payload["current_report_state"] == "unknown"
    assert payload["report_batch_complete"] is False
    assert output.err == ""
    assert partial_report.read_text(encoding="utf-8") == "incomplete"


def test_successful_batch_still_prints_each_report_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    _prepare_config(tmp_path)

    def fake_report(symbol: str, **_kwargs: object) -> tuple[Path, dict[str, object]]:
        return tmp_path / "outputs" / f"{symbol}.html", {}

    monkeypatch.setattr(cli, "experiment_symbol", fake_report)
    assert cli.main(["experiment-all"]) == 0
    output = capsys.readouterr()
    assert output.out.count("样本外实验报告已生成：") == 2
    assert "sh.510300.html" in output.out
    assert "sh.510500.html" in output.out
    assert output.err == ""
