"""Read-only dataset identity check before a multi-command research session."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from finance_lab import cli
from finance_lab.config import get_paths
from finance_lab.data_manifest import generate_dataset_manifest
from finance_lab.market_data_lock import market_data_lock
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated


def _dataset(root: Path) -> str:
    paths = get_paths(root)
    save_curated(make_synthetic_daily_prices(periods=140), paths)
    return generate_dataset_manifest(paths).dataset_id


def _run_check(expected: str) -> int:
    return cli.main(["dataset-check", "--expected-dataset-id", expected])


def test_id_is_required() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["dataset-check"])
    assert raised.value.code == 2


@pytest.mark.parametrize("value", ["a" * 63, "a" * 65, "g" * 64, "", "a" * 63 + " "])
def test_invalid_id_is_rejected(value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["dataset-check", "--expected-dataset-id", value])
    assert raised.value.code == 2


def test_uppercase_id_is_normalized() -> None:
    args = cli.build_parser().parse_args(["dataset-check", "--expected-dataset-id", "A" * 64])
    assert args.expected_dataset_id == "a" * 64


def test_matching_id_returns_one_json_and_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = _dataset(tmp_path)
    paths = get_paths(tmp_path)
    curated_before = next(paths.curated.glob("*.parquet")).read_bytes()
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert _run_check(expected) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert output.out.count('"dataset_id"') == 1
    assert payload["ok"] is True
    assert payload["status"] == "verified"
    assert payload["dataset_id"] == expected
    assert payload["expected_dataset_id"] == expected
    assert payload["total_rows"] == 140
    assert payload["file_count"] == 1
    assert payload["data_committed"] is False
    assert payload["health_status"] == "warning"  # Synthetic, unadjusted and old.
    assert output.err == ""
    assert not list(paths.outputs.iterdir())
    assert next(paths.curated.glob("*.parquet")).read_bytes() == curated_before


def test_changed_dataset_fails_before_any_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = _dataset(tmp_path)
    paths = get_paths(tmp_path)
    save_curated(make_synthetic_daily_prices(periods=141), paths)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert _run_check(expected) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "dataset_id_mismatch"
    assert "数据集核验未完成" in payload["message"]
    assert payload["expected_dataset_id"] == expected
    assert payload["actual_dataset_id"] != expected
    assert not list(paths.outputs.iterdir())


def test_missing_dataset_is_safe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert _run_check("a" * 64) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "dataset_preflight_failed"
    assert "数据集核验未完成" in payload["message"]
    assert payload["data_committed"] is False


def test_unhealthy_matching_dataset_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    prices.loc[0, "close"] = -1.0
    prices.to_parquet(paths.curated / "demo_000300.parquet", index=False)
    expected = generate_dataset_manifest(paths).dataset_id
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert _run_check(expected) != 0
    assert json.loads(capsys.readouterr().out)["error_code"] == "dataset_health_failed"
    assert not list(paths.outputs.iterdir())


def test_busy_lock_fails_before_manifest_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = get_paths(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cli, "generate_dataset_manifest", lambda *_args: pytest.fail("manifest read started")
    )
    ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with market_data_lock(paths.data):
            ready.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert ready.wait(timeout=5)
    try:
        result = _run_check("a" * 64)
    finally:
        release.set()
        holder.join(timeout=5)
    assert result != 0
    assert json.loads(capsys.readouterr().out)["error_code"] == "market_snapshot_busy"


def test_internal_manifest_error_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    def fail(_paths: object) -> None:
        raise OSError("C:/secret/private-market-file.parquet")

    monkeypatch.setattr(cli, "generate_dataset_manifest", fail)
    assert _run_check("a" * 64) != 0
    output = capsys.readouterr()
    assert json.loads(output.out)["error_code"] == "dataset_preflight_failed"
    assert "secret" not in output.out + output.err


def test_lock_is_released_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = _dataset(tmp_path)
    paths = get_paths(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert _run_check(expected) == 0
    capsys.readouterr()
    acquired: list[bool] = []

    def competitor() -> None:
        with market_data_lock(paths.data):
            acquired.append(True)

    thread = threading.Thread(target=competitor)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert acquired == [True]
