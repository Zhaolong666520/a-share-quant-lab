"""Missing local inputs should fail before report generation with actionable diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab.cli import main


@pytest.mark.parametrize(
    ("command", "error_code", "stage"),
    [
        ("manifest", "missing_curated_data", "load_curated"),
        ("account", "missing_instruments_config", "load_config"),
        ("account-all", "missing_instruments_config", "load_config"),
        ("experiment-all", "missing_instruments_config", "load_config"),
        ("walk-forward-all", "missing_instruments_config", "load_config"),
        ("cost-stress-all", "missing_instruments_config", "load_config"),
        ("parameter-test-all", "missing_instruments_config", "load_config"),
        ("execution-test-all", "missing_instruments_config", "load_config"),
        ("compare-all", "missing_instruments_config", "load_config"),
    ],
)
def test_missing_required_input_has_safe_nonzero_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    error_code: str,
    stage: str,
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main([command]) != 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == error_code
    assert payload["stage"] == stage
    assert payload["data_committed"] is False
    assert payload["next_action"]
    assert output.err == ""
    assert not list((tmp_path / "outputs").glob("*.html"))
    assert not list((tmp_path / "outputs").glob("*.json"))


@pytest.mark.parametrize(
    "command",
    [
        "account",
        "account-all",
        "experiment-all",
        "walk-forward-all",
        "cost-stress-all",
        "parameter-test-all",
        "execution-test-all",
        "compare-all",
    ],
)
@pytest.mark.parametrize(
    "config_text",
    [
        '{"secret":"do-not-print"',
        "[]",
        '[{"code":"510300"}]',
    ],
    ids=["invalid_json", "empty_list", "missing_fields"],
)
def test_invalid_instruments_config_has_safe_nonzero_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    config_text: str,
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    config_path = tmp_path / "config" / "instruments.json"
    config_path.parent.mkdir()
    config_path.write_text(config_text, encoding="utf-8")

    assert main([command]) == 3
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_instruments_config"
    assert payload["stage"] == "load_config"
    assert payload["data_committed"] is False
    assert payload["next_action"]
    assert "do-not-print" not in output.out
    assert output.err == ""
    assert not list((tmp_path / "outputs").glob("*.html"))
    assert not list((tmp_path / "outputs").glob("*.json"))


@pytest.mark.parametrize(
    "command",
    [
        "account",
        "backtest",
        "experiment",
        "walk-forward",
        "cost-stress",
        "parameter-test",
        "execution-test",
        "compare",
    ],
)
def test_missing_requested_symbol_data_has_safe_nonzero_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    config_path = tmp_path / "config" / "instruments.json"
    config_path.parent.mkdir()
    config_path.write_text(
        '[{"code":"510300","exchange":"sh","name":"test","kind":"etf"}]',
        encoding="utf-8",
    )

    assert main([command, "--symbol", "sh.510300"]) == 3
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error_code"] == "missing_symbol_data"
    assert payload["stage"] == "load_curated"
    assert payload["data_committed"] is False
    assert payload["next_action"]
    assert output.err == ""
    assert not list((tmp_path / "outputs").glob("*.html"))
    assert not list((tmp_path / "outputs").glob("*.json"))


def test_market_quarantine_precedes_missing_symbol_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    state_path = tmp_path / "data" / "market_update_state.json"
    state_path.parent.mkdir()
    state_path.write_text("{}", encoding="utf-8")

    assert main(["backtest", "--symbol", "sh.510300"]) != 0
    output = capsys.readouterr()
    assert "missing_symbol_data" not in output.out
    assert "data-update-status" in output.err
    assert not list((tmp_path / "outputs").glob("*.html"))
