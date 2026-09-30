from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from finance_lab import pipeline
from finance_lab.config import get_paths
from finance_lab.pipeline import (
    cost_stress_symbol,
    create_demo_report,
    execution_test_symbol,
    parameter_test_symbol,
    update_market_data,
    walk_forward_symbol,
)
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def test_demo_creates_report_without_network(tmp_path: Path) -> None:
    html_path, metrics = create_demo_report(tmp_path)
    assert html_path.exists()
    assert "合成演示数据" in html_path.read_text(encoding="utf-8")
    assert (tmp_path / "outputs" / "demo_000300_sma_equity.csv").is_file()
    assert int(metrics["observations"]) > 100


def test_walk_forward_pipeline_reads_curated_data_and_writes_report(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    save_curated(prices, get_paths(tmp_path))

    report, payload = walk_forward_symbol(
        "demo.000300",
        first_oos_date=date(2023, 1, 1),
        root=tmp_path,
    )

    assert report.exists()
    fold_count = payload["fold_count"]
    assert isinstance(fold_count, int)
    assert fold_count >= 2
    execution_checks = payload["execution_checks"]
    assert isinstance(execution_checks, dict)
    assert bool(execution_checks["passed"])


def test_cost_stress_pipeline_reads_curated_data_and_writes_report(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    save_curated(prices, get_paths(tmp_path))

    report, payload = cost_stress_symbol(
        "demo.000300",
        first_oos_date=date(2023, 1, 1),
        cost_scenarios_bps=(5.0, 20.0),
        root=tmp_path,
    )

    assert report.exists()
    assert bool(payload["monotonic_non_increasing"])
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list)
    assert len(scenarios) == 2


def test_parameter_pipeline_reads_curated_data_and_writes_report(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    save_curated(prices, get_paths(tmp_path))

    report, payload = parameter_test_symbol(
        "demo.000300",
        first_oos_date=date(2023, 1, 1),
        short_windows=(10, 20),
        long_windows=(40, 60),
        reference_pair=(20, 60),
        root=tmp_path,
    )

    assert report.exists()
    total_scenarios = payload["total_scenarios"]
    assert isinstance(total_scenarios, int)
    assert total_scenarios == 4
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list)
    assert len(scenarios) == 4


def test_execution_pipeline_reads_curated_data_and_writes_report(tmp_path: Path) -> None:
    prices = make_synthetic_daily_prices(periods=1000)
    save_curated(prices, get_paths(tmp_path))
    config = tmp_path / "config"
    config.mkdir()
    (config / "instruments.json").write_text(
        json.dumps(
            [
                {
                    "code": "000300",
                    "exchange": "demo",
                    "name": "Demo Index",
                    "kind": "index",
                }
            ]
        ),
        encoding="utf-8",
    )

    report, payload = execution_test_symbol(
        "demo.000300",
        first_oos_date=date(2023, 1, 1),
        root=tmp_path,
    )

    assert report.exists()
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, list)
    assert len(scenarios) == 3
    assert all(bool(item["execution_checks"]["passed"]) for item in scenarios)
    assert "指数本身不能直接交易" in report.read_text(encoding="utf-8")


def test_update_summary_write_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pipeline, "load_instruments", lambda root: [])
    update_market_data(date(2024, 1, 1), date(2024, 1, 2), tmp_path)
    summary = get_paths(tmp_path).outputs / "update_summary.json"
    expected = summary.read_text(encoding="utf-8")
    original_write_text = Path.write_text

    def interrupted_write_text(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        original_write_text(
            self,
            "partial json",
            encoding=encoding,
            errors=errors,
            newline=newline,
        )
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(Path, "write_text", interrupted_write_text)

    with pytest.raises(OSError, match="simulated"):
        update_market_data(date(2024, 1, 1), date(2024, 1, 3), tmp_path)

    assert summary.read_text(encoding="utf-8") == expected
    assert not list(summary.parent.glob(".*.tmp"))
