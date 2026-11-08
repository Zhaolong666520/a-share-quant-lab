"""A failed report publication must not look like a successful run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab import atomic_io
from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


@pytest.mark.parametrize(
    "argv",
    [["demo"], ["backtest", "--symbol", "demo.000300"]],
    ids=["demo", "backtest"],
)
def test_report_write_failure_is_structured_and_keeps_existing_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    curated = tmp_path / "data" / "curated" / "demo_000300.parquet"
    if argv[0] == "backtest":
        save_curated(make_synthetic_daily_prices(periods=180), get_paths(tmp_path))
        before = curated.read_bytes()
    else:
        before = None

    original_replace = atomic_io.os.replace

    def fail_json_replace(source: object, destination: object) -> None:
        if str(destination).endswith("_metrics.json"):
            raise OSError("private disk diagnostic must not be printed")
        original_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", fail_json_replace)
    assert main(argv) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["status"] == "failed"
    assert payload["error_code"] == "report_write_failed"
    assert payload["stage"] == "write_report"
    assert payload["data_committed"] is False
    assert payload["report_complete"] is False
    assert payload["current_report_state"] == "unknown"
    assert "report-verify" in payload["next_action"]
    assert "private disk diagnostic" not in output.out + output.err
    assert output.err == ""
    assert not list((tmp_path / "outputs").glob("*.bundle.json"))
    if before is not None:
        assert curated.read_bytes() == before
