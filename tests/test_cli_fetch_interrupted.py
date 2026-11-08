"""An interrupted update does not imply that market-data writes were rolled back."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from finance_lab import cli, pipeline
from finance_lab.cli import main
from finance_lab.market_data_state import market_update_status
from tests.paper_helpers import make_paper_test_project


@pytest.mark.parametrize("command", ["fetch", "all"])
def test_update_summary_write_failure_reports_uncertain_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    previous_summary = (paths.outputs / "update_summary.json").read_bytes()
    previous_curated = (paths.curated / "sh_510300.parquet").read_bytes()
    updated = prices.copy()
    updated["volume"] += 1
    updated["ingested_at"] = "2030-01-01T00:00:00Z"
    monkeypatch.setattr(pipeline, "fetch_akshare", lambda *args: updated.copy())
    monkeypatch.setattr(pipeline, "fetch_baostock", lambda *args: updated.copy())

    def fail_summary(path: Path, content: str, encoding: str = "utf-8") -> None:
        if path.name == "update_summary.json":
            raise OSError("private summary disk path")
        raise AssertionError("unexpected write")

    monkeypatch.setattr(pipeline, "write_text_atomic", fail_summary)

    def forbidden_backtest(*args: object, **kwargs: object) -> None:
        raise AssertionError("report stage must not start after an interrupted update")

    monkeypatch.setattr(cli, "backtest_symbol", forbidden_backtest)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(
        [command, "--start", "2022-01-01", "--end", prices["trade_date"].max().date().isoformat()]
    ) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "market_update_interrupted"
    assert payload["stage"] == "update_market_data"
    assert payload["data_committed"] is None
    assert payload["market_update_status"] == "failed"
    assert payload["market_data_blocked"] is True
    assert payload["market_update_phase"] == "summary"
    assert payload["summary_state"] == "unknown"
    assert "data-update-status" in payload["next_action"]
    assert "private summary disk path" not in output.out + output.err
    assert "回测报告已生成" not in output.out
    assert output.err == ""
    assert (paths.outputs / "update_summary.json").read_bytes() == previous_summary
    assert (paths.curated / "sh_510300.parquet").read_bytes() != previous_curated
    assert pd.read_parquet(paths.curated / "sh_510300.parquet")["volume"].equals(
        updated["volume"]
    )
    diagnostic = market_update_status(paths)
    assert diagnostic["blocked"] is True
    assert diagnostic["reason"] == "incomplete_batch"
