from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from finance_lab.config import get_paths
from finance_lab.pipeline import (
    cost_stress_symbol,
    create_demo_report,
    execution_test_symbol,
    parameter_test_symbol,
    walk_forward_symbol,
)
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def test_demo_creates_report_without_network(tmp_path: Path) -> None:
    html_path, metrics = create_demo_report(tmp_path)
    assert html_path.exists()
    assert "合成演示数据" in html_path.read_text(encoding="utf-8")
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
