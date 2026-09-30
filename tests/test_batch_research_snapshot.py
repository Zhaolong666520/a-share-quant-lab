"""A CLI research batch must not mix cooperative market-data generations."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from finance_lab import cli
from finance_lab.config import get_paths
from finance_lab.market_data_lock import MarketDataLockError, market_data_lock

_BATCH_COMMANDS = [
    ("account-all", "account_backtest_symbol"),
    ("experiment-all", "experiment_symbol"),
    ("walk-forward-all", "walk_forward_symbol"),
    ("cost-stress-all", "cost_stress_symbol"),
    ("parameter-test-all", "parameter_test_symbol"),
    ("execution-test-all", "execution_test_symbol"),
    ("compare-all", "strategy_compare_symbol"),
]


def _prepare_config(root: Path) -> None:
    config = root / "config" / "instruments.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            [
                {"code": "510300", "exchange": "sh", "name": "A", "kind": "etf"},
                {"code": "510500", "exchange": "sh", "name": "B", "kind": "etf"},
            ]
        ),
        encoding="utf-8",
    )


def _competitor_outcome(data_directory: Path) -> str:
    outcomes: list[str] = []

    def attempt() -> None:
        try:
            with market_data_lock(data_directory):
                outcomes.append("acquired")
        except MarketDataLockError:
            outcomes.append("blocked")

    worker = threading.Thread(target=attempt)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    return outcomes[0]


@pytest.mark.parametrize(("command", "function_name"), _BATCH_COMMANDS)
def test_research_batch_holds_one_market_snapshot_across_symbols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    function_name: str,
) -> None:
    paths = get_paths(tmp_path)
    _prepare_config(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    observed: list[str] = []

    def fake_report(symbol: str, **_kwargs: object) -> tuple[Path, dict[str, object]]:
        observed.append(_competitor_outcome(paths.data))
        return paths.outputs / f"{symbol}.html", {}

    monkeypatch.setattr(cli, function_name, fake_report)
    assert cli.main([command]) == 0
    capsys.readouterr()

    assert observed == ["blocked", "blocked"]
    assert _competitor_outcome(paths.data) == "acquired"


def test_all_keeps_snapshot_bound_from_update_through_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = get_paths(tmp_path)
    _prepare_config(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    observed: list[str] = []

    def update(*_args: object, **_kwargs: object) -> dict[str, object]:
        observed.append(_competitor_outcome(paths.data))
        return {
            "status": "success",
            "successful_instruments": 2,
            "failed_sources": 0,
            "records": [
                {"symbol": "sh.510300", "rows": 1},
                {"symbol": "sh.510500", "rows": 1},
            ],
        }

    def report(symbol: str) -> tuple[Path, dict[str, object]]:
        observed.append(_competitor_outcome(paths.data))
        return paths.outputs / f"{symbol}.html", {}

    monkeypatch.setattr(cli, "update_market_data", update)
    monkeypatch.setattr(cli, "backtest_symbol", report)
    assert cli.main(["all"]) == 0
    capsys.readouterr()

    assert observed == ["blocked", "blocked", "blocked"]
    assert _competitor_outcome(paths.data) == "acquired"


@pytest.mark.parametrize("command", ["experiment-all", "all"])
def test_busy_batch_fails_before_any_report_or_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    paths = get_paths(tmp_path)
    _prepare_config(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    ready = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with market_data_lock(paths.data):
            ready.set()
            assert release.wait(timeout=5)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("batch action must not start")

    monkeypatch.setattr(cli, "experiment_symbol", forbidden)
    monkeypatch.setattr(cli, "update_market_data", forbidden)
    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert ready.wait(timeout=5)
    try:
        exit_code = cli.main([command])
    finally:
        release.set()
        holder.join(timeout=5)
    assert not holder.is_alive()

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code == 1
    assert payload["error_code"] == "market_snapshot_busy"
    assert payload["stage"] == "acquire_market_snapshot"
    assert payload["data_committed"] is False
    assert payload["reports_confirmed"] == []
    assert payload["report_batch_complete"] is False
    assert output.err == ""


def test_batch_failure_releases_snapshot_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = get_paths(tmp_path)
    _prepare_config(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    def fail_report(*_args: object, **_kwargs: object) -> None:
        raise OSError("private path must not reach output")

    monkeypatch.setattr(cli, "experiment_symbol", fail_report)
    assert cli.main(["experiment-all"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["error_code"] == "batch_report_failed"
    assert "private path" not in output.out + output.err
    assert _competitor_outcome(paths.data) == "acquired"
