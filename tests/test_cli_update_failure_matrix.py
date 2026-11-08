"""Ten interruption checkpoints across fetch and all use only synthetic data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import duckdb
import pytest

from finance_lab import cli, market_data_state, pipeline
from finance_lab.market_data_state import market_update_status
from tests.paper_helpers import make_paper_test_project

Command = Literal["fetch", "all"]
FailureStage = Literal["initial_state", "raw", "curated", "database", "commit"]


@pytest.mark.parametrize("command", ["fetch", "all"])
@pytest.mark.parametrize(
    "failure_stage",
    ["initial_state", "raw", "curated", "database", "commit"],
)
def test_update_interruption_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: Command,
    failure_stage: FailureStage,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    old_curated = (paths.curated / "sh_510300.parquet").read_bytes()
    old_summary = (paths.outputs / "update_summary.json").read_bytes()
    updated = prices.copy()
    updated["volume"] += 1
    updated["ingested_at"] = "2030-01-01T00:00:00Z"
    fetch_calls: list[str] = []
    report_calls: list[str] = []

    def synthetic_fetch(*args: object) -> object:
        fetch_calls.append("fetch")
        return updated.copy()

    def forbidden_report(*args: object, **kwargs: object) -> None:
        report_calls.append("report")
        raise AssertionError("report stage ran after update failure")

    monkeypatch.setattr(pipeline, "fetch_akshare", synthetic_fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", synthetic_fetch)
    monkeypatch.setattr(cli, "backtest_symbol", forbidden_report)

    if failure_stage in {"initial_state", "commit"}:
        original_publish = market_data_state._publish

        def fail_state(paths_arg: object, state: market_data_state.MarketUpdateState) -> None:
            if failure_stage == "initial_state" and state.phase == "preparing":
                raise OSError("private state disk path")
            if failure_stage == "commit" and state.status == "completed":
                raise OSError("private state disk path")
            original_publish(paths_arg, state)

        monkeypatch.setattr(market_data_state, "_publish", fail_state)
    elif failure_stage == "raw":
        def fail_raw(*args: object) -> None:
            raise OSError("private raw disk path")

        monkeypatch.setattr(pipeline, "save_raw_snapshot", fail_raw)
    elif failure_stage == "curated":
        def fail_curated(*args: object) -> None:
            raise OSError("private curated disk path")

        monkeypatch.setattr(pipeline, "save_curated", fail_curated)
    elif failure_stage == "database":
        def fail_database(*args: object) -> None:
            raise duckdb.IOException("private database disk path")

        monkeypatch.setattr(pipeline, "sync_duckdb", fail_database)

    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    exit_code = cli.main(
        [command, "--start", "2022-01-01", "--end", prices["trade_date"].max().date().isoformat()]
    )
    output = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "market_update_interrupted"
    assert payload["stage"] == "update_market_data"
    assert payload["data_committed"] is None
    assert payload["summary_state"] == "unknown"
    if command == "all":
        assert payload["report_batch_complete"] is False
        assert payload["reports_confirmed"] == []
    assert "data-update-status" in payload["next_action"]
    assert "private" not in output.out + output.err
    assert "报告已生成" not in output.out
    assert output.err == ""
    assert report_calls == []

    diagnostic = market_update_status(paths)
    if failure_stage == "initial_state":
        assert fetch_calls == []
        assert diagnostic["status"] == "legacy"
        assert diagnostic["blocked"] is False
        assert payload["market_update_status"] == "legacy"
        assert payload["market_data_blocked"] is False
        assert payload["market_update_phase"] is None
    else:
        assert fetch_calls
        assert diagnostic["status"] == "failed"
        assert diagnostic["blocked"] is True
        assert diagnostic["reason"] == "incomplete_batch"
        assert payload["market_update_status"] == "failed"
        assert payload["market_data_blocked"] is True
        assert payload["market_update_phase"] == failure_stage

    curated = (paths.curated / "sh_510300.parquet").read_bytes()
    summary = (paths.outputs / "update_summary.json").read_bytes()
    assert (curated == old_curated) is (failure_stage in {"initial_state", "raw", "curated"})
    assert (summary == old_summary) is (failure_stage != "commit")
