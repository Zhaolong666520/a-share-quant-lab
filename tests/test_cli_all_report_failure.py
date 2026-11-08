"""The combined command must distinguish committed data from failed reports."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from finance_lab import atomic_io, cli
from finance_lab.config import get_paths
from finance_lab.pipeline import ReportWriteError
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def test_all_reports_data_commit_when_report_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    config = tmp_path / "config" / "instruments.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            [{"code": "000300", "exchange": "demo", "name": "合成示例", "kind": "index"}]
        ),
        encoding="utf-8",
    )
    saved_bytes: list[bytes] = []

    def synthetic_update(_start: date, _end: date) -> dict[str, object]:
        path = save_curated(make_synthetic_daily_prices(periods=180), get_paths(tmp_path))
        saved_bytes.append(path.read_bytes())
        return {
            "status": "success",
            "successful_instruments": 1,
            "records": [{"symbol": "demo.000300", "rows": 180}],
        }

    monkeypatch.setattr(cli, "update_market_data", synthetic_update)
    original_replace = atomic_io.os.replace

    def fail_json_replace(source: object, destination: object) -> None:
        if str(destination).endswith("_metrics.json"):
            raise OSError("private disk diagnostic must not be printed")
        original_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", fail_json_replace)
    assert cli.main(["all"]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["ok"] is False
    assert result["error_code"] == "report_write_failed_after_data_commit"
    assert result["stage"] == "write_report"
    assert result["data_committed"] is True
    assert result["market_update_status"] == "success"
    assert result["report_batch_complete"] is False
    assert result["reports_confirmed"] == []
    assert result["failed_symbol"] == "demo.000300"
    assert result["current_report_state"] == "unknown"
    assert "backtest" in result["next_action"]
    assert "private disk diagnostic" not in output.out + output.err
    assert output.err == ""
    assert (tmp_path / "data" / "curated" / "demo_000300.parquet").read_bytes() == (
        saved_bytes[0]
    )
    assert not list((tmp_path / "outputs").glob("*.bundle.json"))


def test_all_failure_lists_only_reports_confirmed_before_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    config = tmp_path / "config" / "instruments.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            [
                {"code": "000300", "exchange": "demo", "name": "甲", "kind": "index"},
                {"code": "000500", "exchange": "demo", "name": "乙", "kind": "index"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "update_market_data",
        lambda _start, _end: {
            "status": "success",
            "successful_instruments": 2,
            "records": [
                {"symbol": "demo.000300", "rows": 180},
                {"symbol": "demo.000500", "rows": 180},
            ],
        },
    )
    first_report = tmp_path / "outputs" / "first.html"

    def fake_backtest(symbol: str) -> tuple[Path, dict[str, float | int]]:
        if symbol == "demo.000300":
            first_report.parent.mkdir(exist_ok=True)
            first_report.write_text("complete", encoding="utf-8")
            return first_report, {}
        raise ReportWriteError("do not expose")

    monkeypatch.setattr(cli, "backtest_symbol", fake_backtest)
    assert cli.main(["all"]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["reports_confirmed"] == [
        {"symbol": "demo.000300", "report": str(first_report)}
    ]
    assert result["failed_symbol"] == "demo.000500"
    assert "回测报告已生成" not in output.out
    assert "do not expose" not in output.out + output.err
    assert first_report.read_text(encoding="utf-8") == "complete"
