"""A completion marker detects mixed or missing files in a historical report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab import atomic_io
from finance_lab.backtest import run_backtest
from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.report import write_backtest_report
from finance_lab.report_bundle import report_marker_path, verify_report_bundle
from finance_lab.sample import make_synthetic_daily_prices


def _write_sample_report(root: Path, *, cost_bps: float = 5.0) -> tuple[Path, Path, Path]:
    prices = make_synthetic_daily_prices(periods=180)
    result = run_backtest(prices, cost_bps=cost_bps)
    return write_backtest_report(result, get_paths(root))


def test_backtest_report_publishes_verifiable_completion_marker(tmp_path: Path) -> None:
    html_path, chart_path, json_path = _write_sample_report(tmp_path)
    marker = report_marker_path(html_path)
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert {item["name"] for item in payload["files"]} == {
        html_path.name,
        chart_path.name,
        html_path.name.replace("_report.html", "_equity.csv"),
        json_path.name,
    }
    assert verify_report_bundle(html_path)["status"] == "verified"


@pytest.mark.parametrize("change", ["tamper", "remove"])
def test_report_bundle_rejects_changed_or_missing_member(tmp_path: Path, change: str) -> None:
    html_path, chart_path, _ = _write_sample_report(tmp_path)
    if change == "tamper":
        chart_path.write_bytes(b"not a chart")
        expected_status = "hash_mismatch"
    else:
        chart_path.unlink()
        expected_status = "member_missing"

    result = verify_report_bundle(html_path)
    assert result["ok"] is False
    assert result["status"] == expected_status


def test_failed_rerun_does_not_revalidate_old_completion_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_path, _, json_path = _write_sample_report(tmp_path)
    marker = report_marker_path(html_path)
    previous_marker = marker.read_bytes()
    original_replace = atomic_io.os.replace

    def fail_json_replace(source: object, destination: object) -> None:
        if Path(str(destination)) == json_path:
            raise OSError("simulated report interruption")
        original_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", fail_json_replace)
    with pytest.raises(OSError, match="simulated report interruption"):
        _write_sample_report(tmp_path, cost_bps=25.0)

    assert marker.read_bytes() == previous_marker
    assert verify_report_bundle(html_path)["ok"] is False


def test_report_verify_cli_distinguishes_complete_and_changed_reports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    html_path, chart_path, _ = _write_sample_report(tmp_path)
    assert main(["report-verify", "--report", str(html_path)]) == 0
    complete = capsys.readouterr()
    assert json.loads(complete.out)["status"] == "verified"
    assert complete.err == ""

    chart_path.write_bytes(b"changed")
    assert main(["report-verify", "--report", str(html_path)]) != 0
    changed = capsys.readouterr()
    assert json.loads(changed.out)["status"] == "hash_mismatch"
    assert changed.err == ""


def test_report_verify_cli_rejects_missing_and_unsafe_markers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    html_path = tmp_path / "missing.html"
    assert main(["report-verify", "--report", str(html_path)]) != 0
    missing = capsys.readouterr()
    assert json.loads(missing.out)["status"] == "marker_missing"
    assert missing.err == ""

    marker = report_marker_path(html_path)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_name": html_path.name,
                "files": [
                    {"name": "../outside", "sha256": "0" * 64, "size_bytes": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["report-verify", "--report", str(html_path)]) != 0
    unsafe = capsys.readouterr()
    assert json.loads(unsafe.out)["status"] == "invalid_marker"
    assert unsafe.err == ""
