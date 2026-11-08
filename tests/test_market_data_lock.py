from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance_lab import pipeline, storage
from finance_lab.config import get_paths
from finance_lab.data_manifest import generate_dataset_manifest, read_update_summary
from finance_lab.market_data_lock import market_data_lock
from finance_lab.paper_pipeline import load_paper_market_snapshot
from finance_lab.sample import make_synthetic_daily_prices
from tests.paper_helpers import make_paper_test_project


def _child(code: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONIOENCODING": "utf-8",
            "FINANCE_LAB_ROOT": str(root),
        },
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize("competitor", ["thread", "process"])
def test_competing_incremental_save_is_rejected_then_retry_preserves_both_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    competitor: str,
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=150)
    storage.save_curated(prices.head(120), paths)
    original_write = storage.write_parquet_atomic
    owner_thread = threading.get_ident()
    attempted = False
    outcomes: list[str] = []
    workers: list[threading.Thread] = []

    def other_writer() -> None:
        try:
            storage.save_curated(prices.iloc[130:140], paths)
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        else:
            outcomes.append("saved")

    def write_after_competitor(frame: pd.DataFrame, path: Path) -> None:
        nonlocal attempted
        if threading.get_ident() == owner_thread and not attempted:
            attempted = True
            if competitor == "thread":
                worker = threading.Thread(target=other_writer)
                workers.append(worker)
                worker.start()
                worker.join(timeout=10)
                assert not worker.is_alive(), "competing writer must fail promptly"
            else:
                result = _child(
                    """
from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated
try:
    save_curated(make_synthetic_daily_prices(periods=150).iloc[130:140], get_paths())
except Exception as exc:
    print(type(exc).__name__)
else:
    print('saved')
""",
                    tmp_path,
                )
                assert result.returncode == 0, result.stderr
                outcomes.append(result.stdout.strip())
        original_write(frame, path)

    monkeypatch.setattr(storage, "write_parquet_atomic", write_after_competitor)
    try:
        storage.save_curated(prices.iloc[120:130], paths)
    finally:
        for worker in workers:
            worker.join(timeout=10)
    current = storage.read_curated("demo.000300", paths)
    assert outcomes == ["MarketDataLockError"], (
        f"competing writer {outcomes}; only {len(current)} rows remain after two increments"
    )
    assert len(current) == 130

    storage.save_curated(prices.iloc[130:140], paths)
    restored = storage.read_curated("demo.000300", paths)
    assert list(restored["trade_date"]) == list(prices.head(140)["trade_date"])


def _thread_outcome(operation: Callable[[], object]) -> str:
    outcomes: list[str] = []

    def worker() -> None:
        try:
            operation()
        except Exception as exc:
            outcomes.append(type(exc).__name__)
        else:
            outcomes.append("succeeded")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "lock contention must return immediately"
    return outcomes[0]


@pytest.mark.parametrize(
    "operation",
    ["raw", "curated", "sync", "read", "manifest", "summary", "validate", "update", "paper"],
)
def test_public_operations_reject_competing_thread_before_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    last_date = prices["trade_date"].max().date()
    calls: list[str] = []

    def fetch(*args: object) -> pd.DataFrame:
        calls.append("fetch")
        return prices

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    actions: dict[str, Callable[[], object]] = {
        "raw": lambda: storage.save_raw_snapshot(prices, paths),
        "curated": lambda: storage.save_curated(prices, paths),
        "sync": lambda: storage.sync_duckdb(paths),
        "read": lambda: storage.read_curated("sh.510300", paths),
        "manifest": lambda: generate_dataset_manifest(paths, as_of_date=last_date),
        "summary": lambda: read_update_summary(paths),
        "validate": lambda: pipeline.validate_curated_data(tmp_path),
        "update": lambda: pipeline.update_market_data(date(2022, 1, 1), last_date, tmp_path),
        "paper": lambda: load_paper_market_snapshot(paths, last_date, 3),
    }
    curated = paths.curated / "sh_510300.parquet"
    before = curated.read_bytes()
    summary_before = (paths.outputs / "update_summary.json").read_bytes()

    with market_data_lock(paths.data):
        assert _thread_outcome(actions[operation]) == "MarketDataLockError"

    assert calls == []
    assert curated.read_bytes() == before
    assert (paths.outputs / "update_summary.json").read_bytes() == summary_before
    assert not list(paths.raw.rglob("*.parquet"))
    assert not paths.database.exists()


@pytest.mark.parametrize("phase", ["fetch", "summary"])
def test_update_holds_lock_across_fetch_and_summary_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    checked: list[str] = []
    write_summary = pipeline.write_text_atomic

    def checkpoint() -> None:
        if not checked:
            checked.append(_thread_outcome(lambda: storage.save_curated(prices, paths)))

    def fetch(*args: object) -> pd.DataFrame:
        if phase == "fetch":
            checkpoint()
        return prices

    def summary(path: Path, content: str, *, encoding: str = "utf-8") -> None:
        if phase == "summary":
            checkpoint()
        write_summary(path, content, encoding=encoding)

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    monkeypatch.setattr(pipeline, "write_text_atomic", summary)

    result = pipeline.update_market_data(
        date(2022, 1, 1),
        prices["trade_date"].max().date(),
        tmp_path,
    )

    assert checked == ["MarketDataLockError"]
    assert result["database_rows"] == len(prices)
    assert result["successful_instruments"] == 1
    assert read_update_summary(paths).records[0].rows == len(prices)


def test_nested_failure_keeps_outer_lock_then_releases_it(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    child_code = """
from finance_lab.config import get_paths
from finance_lab.market_data_lock import market_data_lock
with market_data_lock(get_paths().data):
    print('acquired')
"""
    with pytest.raises(ValueError, match="outer failure"):
        with market_data_lock(paths.data):
            with pytest.raises(ValueError, match="inner failure"):
                with market_data_lock(paths.data / ".." / "data"):
                    raise ValueError("inner failure")
            assert (
                _thread_outcome(
                    lambda: storage.save_curated(
                        make_synthetic_daily_prices(periods=120),
                        paths,
                    )
                )
                == "MarketDataLockError"
            )
            blocked = _child(child_code, tmp_path)
            assert blocked.returncode != 0
            assert "MarketDataLockError" in blocked.stderr
            raise ValueError("outer failure")

    result = _child(child_code, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "acquired"
    # The lock file stays present; its presence alone is not a busy condition.
    assert (paths.data / "market_data.lock").exists()
    with market_data_lock(paths.data):
        pass


def test_different_projects_do_not_block_each_other(tmp_path: Path) -> None:
    left = get_paths(tmp_path / "left")
    right = get_paths(tmp_path / "right")
    with market_data_lock(left.data):
        assert (
            _thread_outcome(
                lambda: storage.save_curated(
                    make_synthetic_daily_prices(periods=120),
                    right,
                )
            )
            == "succeeded"
        )
        with market_data_lock(right.data):
            pass


def test_process_exit_releases_os_lock_without_deleting_file(tmp_path: Path) -> None:
    result = _child(
        """
import os
from finance_lab.config import get_paths
from finance_lab.market_data_lock import market_data_lock
with market_data_lock(get_paths().data):
    os._exit(7)
""",
        tmp_path,
    )
    assert result.returncode == 7
    paths = get_paths(tmp_path)
    assert (paths.data / "market_data.lock").exists()
    with market_data_lock(paths.data):
        storage.save_curated(make_synthetic_daily_prices(periods=120), paths)


def test_cli_reports_lock_conflict_with_nonzero_exit(tmp_path: Path) -> None:
    paths, _ = make_paper_test_project(tmp_path)
    with market_data_lock(paths.data):
        result = _child(
            """
from finance_lab.cli import main
raise SystemExit(main(['validate']))
""",
            tmp_path,
        )
    assert result.returncode == 1
    assert "已有行情数据任务正在运行" in result.stderr


def test_daily_automation_records_lock_conflict_without_starting_fetch(tmp_path: Path) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    last_date = prices["trade_date"].max().date().isoformat()
    with market_data_lock(paths.data):
        result = _child(
            f"""
import json
from datetime import date
from finance_lab import pipeline
from finance_lab.paper_automation import run_paper_daily_automation
def forbidden_fetch(*args):
    raise AssertionError('fetch must not start during lock contention')
pipeline.fetch_akshare = forbidden_fetch
pipeline.fetch_baostock = forbidden_fetch
result = run_paper_daily_automation(as_of_date=date.fromisoformat({last_date!r}))
print(json.dumps(result.payload))
raise SystemExit(result.exit_code)
""",
            tmp_path,
        )
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["failed_step"] == "market_data_update"
    assert payload["error"]["type"] == "MarketDataLockError"
    assert payload["operation_status"] is None
    assert not paths.database.exists()
    archives = list((paths.outputs / "paper_automation_runs").glob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8")) == payload
