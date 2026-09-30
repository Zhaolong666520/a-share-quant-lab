"""Manifest report failures must not alter the curated input or claim success."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab import atomic_io
from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def test_manifest_report_write_failure_is_safe_and_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    prices = make_synthetic_daily_prices(periods=140)
    curated = save_curated(prices, get_paths(tmp_path))
    before = curated.read_bytes()
    as_of = prices["trade_date"].max().date().isoformat()
    original_replace = atomic_io.os.replace

    def fail_html_replace(source: object, destination: object) -> None:
        if str(destination).endswith("_report.html"):
            raise OSError("private disk path must not be printed")
        original_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", fail_html_replace)
    assert main(["manifest", "--as-of", as_of]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "report_write_failed"
    assert payload["stage"] == "write_report"
    assert payload["data_committed"] is False
    assert payload["report_complete"] is False
    assert "report-verify" in payload["next_action"]
    assert "private disk path" not in output.out + output.err
    assert output.err == ""
    assert curated.read_bytes() == before
    assert not list((tmp_path / "outputs").glob("*.bundle.json"))
    assert not list((tmp_path / "outputs").glob("*_report.html"))
