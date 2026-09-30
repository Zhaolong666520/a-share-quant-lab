"""Optional research commands must fail closed when the dataset generation changes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from finance_lab import cli
from finance_lab.config import get_paths
from finance_lab.data_manifest import generate_dataset_manifest
from finance_lab.market_data_lock import MarketDataLockError, market_data_lock
from finance_lab.report_bundle import verify_report_bundle
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated

_RESEARCH_COMMANDS = [
    "account",
    "account-all",
    "backtest",
    "experiment",
    "experiment-all",
    "walk-forward",
    "walk-forward-all",
    "cost-stress",
    "cost-stress-all",
    "parameter-test",
    "parameter-test-all",
    "execution-test",
    "execution-test-all",
    "compare",
    "compare-all",
]


def _synthetic_dataset(root: Path) -> str:
    paths = get_paths(root)
    save_curated(make_synthetic_daily_prices(periods=140), paths)
    return generate_dataset_manifest(paths).dataset_id


def _config(root: Path) -> None:
    target = root / "config" / "instruments.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps([{"code": "000300", "exchange": "demo", "name": "Synthetic", "kind": "index"}]),
        encoding="utf-8",
    )


def _competitor(data: Path) -> str:
    result: list[str] = []

    def attempt() -> None:
        try:
            with market_data_lock(data):
                result.append("acquired")
        except MarketDataLockError:
            result.append("blocked")

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    return result[0]


def test_matching_id_binds_single_report_until_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = _synthetic_dataset(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    observed: list[str] = []

    def report(_symbol: str, **_kwargs: object) -> tuple[Path, dict[str, object]]:
        observed.append(_competitor(get_paths(tmp_path).data))
        return tmp_path / "report.html", {}

    monkeypatch.setattr(cli, "backtest_symbol", report)
    assert cli.main(["backtest", "--symbol", "demo.000300", "--expected-dataset-id", expected]) == 0
    capsys.readouterr()
    assert observed == ["blocked"]
    assert _competitor(get_paths(tmp_path).data) == "acquired"


def test_matching_id_binds_batch_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = _synthetic_dataset(tmp_path)
    _config(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    observed: list[str] = []

    def report(_symbol: str, **_kwargs: object) -> tuple[Path, dict[str, object]]:
        observed.append(_competitor(get_paths(tmp_path).data))
        return tmp_path / "report.html", {}

    monkeypatch.setattr(cli, "experiment_symbol", report)
    assert cli.main(["experiment-all", "--expected-dataset-id", expected]) == 0
    capsys.readouterr()
    assert observed == ["blocked"]


@pytest.mark.parametrize("command", ["backtest", "experiment-all"])
def test_changed_dataset_rejects_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    expected = _synthetic_dataset(tmp_path)
    _config(tmp_path)
    save_curated(make_synthetic_daily_prices(periods=141), get_paths(tmp_path))
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("report must not start")

    monkeypatch.setattr(cli, "backtest_symbol", forbidden)
    monkeypatch.setattr(cli, "experiment_symbol", forbidden)
    arguments = [command, "--expected-dataset-id", expected]
    if command == "backtest":
        arguments += ["--symbol", "demo.000300"]
    exit_code = cli.main(arguments)
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code != 0
    assert payload["error_code"] == "dataset_id_mismatch"
    assert payload["expected_dataset_id"] == expected
    assert payload["actual_dataset_id"] != expected
    assert payload["reports_confirmed"] == []
    assert output.err == ""


def test_missing_dataset_fails_without_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "backtest_symbol", lambda _symbol: pytest.fail("report started"))
    assert cli.main(["backtest", "--expected-dataset-id", "a" * 64]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "dataset_preflight_failed"
    assert payload["reports_confirmed"] == []


def test_invalid_id_is_rejected_by_parser() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["backtest", "--expected-dataset-id", "not-a-dataset-id"])
    assert raised.value.code == 2


@pytest.mark.parametrize("command", _RESEARCH_COMMANDS)
def test_all_read_only_research_commands_accept_complete_id(command: str) -> None:
    args = cli.build_parser().parse_args([command, "--expected-dataset-id", "A" * 64])
    assert args.expected_dataset_id == "a" * 64


def test_updating_all_command_does_not_accept_expected_id() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["all", "--expected-dataset-id", "a" * 64])
    assert raised.value.code == 2


def test_unhealthy_dataset_rejects_even_matching_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = get_paths(tmp_path)
    prices = make_synthetic_daily_prices(periods=140)
    prices.loc[0, "close"] = -1.0
    prices.to_parquet(paths.curated / "demo_000300.parquet", index=False)
    expected = generate_dataset_manifest(paths).dataset_id
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cli, "backtest_symbol", lambda *_args, **_kwargs: pytest.fail("report started")
    )

    assert cli.main(["backtest", "--expected-dataset-id", expected]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "dataset_health_failed"


def test_busy_single_research_guard_fails_before_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = _synthetic_dataset(tmp_path)
    paths = get_paths(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(
        cli, "backtest_symbol", lambda *_args, **_kwargs: pytest.fail("report started")
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
        result = cli.main(["backtest", "--expected-dataset-id", expected])
    finally:
        release.set()
        holder.join(timeout=5)
    assert result != 0
    assert json.loads(capsys.readouterr().out)["error_code"] == "market_snapshot_busy"
    assert _competitor(paths.data) == "acquired"


def test_matching_id_generates_verifiable_synthetic_backtest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = _synthetic_dataset(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert cli.main(
        ["backtest", "--symbol", "demo.000300", "--expected-dataset-id", expected]
    ) == 0
    capsys.readouterr()
    reports = list(get_paths(tmp_path).outputs.glob("*.html"))
    assert len(reports) == 1
    assert verify_report_bundle(reports[0])["ok"] is True


def test_manifest_preflight_failure_redacts_internal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    def fail(_paths: object) -> None:
        raise OSError("C:/secret/private-market-file.parquet")

    monkeypatch.setattr(cli, "generate_dataset_manifest", fail)
    assert cli.main(["backtest", "--expected-dataset-id", "a" * 64]) != 0
    output = capsys.readouterr()
    assert json.loads(output.out)["error_code"] == "dataset_preflight_failed"
    assert "secret" not in output.out + output.err
