"""Account backtest report failure does not mean a simulated ledger was committed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab import atomic_io
from finance_lab.cli import main
from tests.paper_helpers import make_paper_test_project


def test_account_report_failure_is_safe_and_does_not_change_curated_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _prices = make_paper_test_project(tmp_path)
    curated = paths.curated / "sh_510300.parquet"
    before = curated.read_bytes()
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    original_replace = atomic_io.os.replace

    def fail_json_replace(source: object, destination: object) -> None:
        if str(destination).endswith("_metrics.json"):
            raise OSError("private account report write detail")
        original_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", fail_json_replace)
    assert main(["account", "--symbol", "sh.510300"]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "report_write_failed"
    assert payload["stage"] == "write_report"
    assert payload["data_committed"] is False
    assert payload["report_complete"] is False
    assert payload["current_report_state"] == "unknown"
    assert "private account report write detail" not in output.out + output.err
    assert output.err == ""
    assert curated.read_bytes() == before
    assert not paths.database.exists()
    assert not list(paths.outputs.glob("*.bundle.json"))
