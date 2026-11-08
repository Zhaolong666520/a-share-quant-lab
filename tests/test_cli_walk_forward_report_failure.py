"""A failed rolling research report must not look like a completed result."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab import atomic_io
from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def test_walk_forward_report_write_failure_is_nonzero_without_changing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    curated = save_curated(make_synthetic_daily_prices(), get_paths(tmp_path))
    before = curated.read_bytes()
    original_replace = atomic_io.os.replace

    def fail_json_replace(source: object, destination: object) -> None:
        if str(destination).endswith("_metrics.json"):
            raise OSError("private rolling report disk path")
        original_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", fail_json_replace)
    assert main(
        ["walk-forward", "--symbol", "demo.000300", "--first-oos-date", "2023-01-01"]
    ) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "report_write_failed"
    assert payload["stage"] == "write_report"
    assert payload["data_committed"] is False
    assert payload["report_complete"] is False
    assert payload["current_report_state"] == "unknown"
    assert "滚动检验报告已生成" not in output.out
    assert "private rolling report disk path" not in output.out + output.err
    assert output.err == ""
    assert curated.read_bytes() == before
    assert not list((tmp_path / "outputs").glob("*.bundle.json"))
