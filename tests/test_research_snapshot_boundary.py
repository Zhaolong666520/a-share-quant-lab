"""A single report must not span two cooperative market-data generations."""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import pytest

from finance_lab import pipeline
from finance_lab.config import get_paths, initialize_instruments
from finance_lab.market_data_lock import MarketDataLockError, market_data_lock
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


class _StopReport(RuntimeError):
    pass


def _concurrent_lock_outcome(data_directory: Path) -> str:
    outcomes: list[str] = []

    def competitor() -> None:
        try:
            with market_data_lock(data_directory):
                outcomes.append("acquired")
        except MarketDataLockError:
            outcomes.append("blocked")

    worker = threading.Thread(target=competitor)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    return outcomes[0]


def test_backtest_holds_market_snapshot_through_calculation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    save_curated(make_synthetic_daily_prices(periods=150), paths)
    outcomes: list[str] = []

    def probe(*_args: object, **_kwargs: object) -> None:
        outcomes.append(_concurrent_lock_outcome(paths.data))
        raise _StopReport("stop before writing report")

    monkeypatch.setattr(pipeline, "run_backtest", probe)
    with pytest.raises(_StopReport):
        pipeline.backtest_symbol("demo.000300", root=tmp_path)

    assert outcomes == ["blocked"]
    assert _concurrent_lock_outcome(paths.data) == "acquired"


@pytest.mark.parametrize(
    ("entry_name", "arguments", "probe_name"),
    [
        ("data_health", (), "generate_dataset_manifest"),
        ("account_backtest_symbol", ("sh.510300",), "generate_dataset_manifest"),
        ("backtest_symbol", ("demo.000300",), "read_curated"),
        ("strategy_compare_symbol", ("demo.000300", date(2023, 1, 1)), "generate_dataset_manifest"),
        ("experiment_symbol", ("demo.000300", date(2023, 1, 1)), "read_curated"),
        ("walk_forward_symbol", ("demo.000300", date(2023, 1, 1)), "read_curated"),
        ("cost_stress_symbol", ("demo.000300", date(2023, 1, 1)), "read_curated"),
        ("parameter_test_symbol", ("demo.000300", date(2023, 1, 1)), "read_curated"),
        ("execution_test_symbol", ("demo.000300", date(2023, 1, 1)), "read_curated"),
    ],
)
def test_every_research_entry_holds_project_lock_at_input_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_name: str,
    arguments: tuple[object, ...],
    probe_name: str,
) -> None:
    paths = get_paths(tmp_path)
    initialize_instruments(tmp_path)
    outcomes: list[str] = []

    def probe(*_args: object, **_kwargs: object) -> None:
        outcomes.append(_concurrent_lock_outcome(paths.data))
        raise _StopReport("stop at input read")

    monkeypatch.setattr(pipeline, probe_name, probe)
    entry = getattr(pipeline, entry_name)
    with pytest.raises(_StopReport):
        entry(*arguments, root=tmp_path)

    assert outcomes == ["blocked"]
    assert _concurrent_lock_outcome(paths.data) == "acquired"


def test_backtest_keeps_snapshot_bound_through_report_publication_with_positional_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = get_paths(tmp_path)
    save_curated(make_synthetic_daily_prices(periods=150), paths)
    outcomes: list[str] = []

    def probe(*_args: object, **_kwargs: object) -> None:
        outcomes.append(_concurrent_lock_outcome(paths.data))
        raise _StopReport("stop during publication")

    monkeypatch.setattr(pipeline, "write_backtest_report", probe)
    with pytest.raises(_StopReport):
        pipeline.backtest_symbol("demo.000300", "sma", 20, 60, 120, 5.0, tmp_path)

    assert outcomes == ["blocked"]
    assert _concurrent_lock_outcome(paths.data) == "acquired"
