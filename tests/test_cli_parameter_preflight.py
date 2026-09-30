"""Malformed CLI arguments must stop before business work begins."""

from __future__ import annotations

from pathlib import Path

import pytest

import finance_lab.cli as cli_module


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch", "--start", "20260925", "--end", "2026-09-26"],
        ["cost-stress", "--costs=nan"],
        ["cost-stress", "--costs=inf,5"],
        ["cost-stress", "--costs=-1,5"],
        ["cost-stress", "--costs=5,,10"],
        ["parameter-test", "--shorts=0,20"],
        ["parameter-test", "--shorts=-1,20"],
        ["parameter-test", "--shorts=10,10"],
        ["parameter-test", "--shorts=10,,20"],
    ],
)
def test_invalid_argument_fails_before_business_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("business dispatch must not start")

    monkeypatch.setattr(cli_module, "update_market_data", forbidden)
    monkeypatch.setattr(cli_module, "cost_stress_symbol", forbidden)
    monkeypatch.setattr(cli_module, "parameter_test_symbol", forbidden)
    with pytest.raises(SystemExit) as raised:
        cli_module.main(argv)
    assert raised.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "error:" in output.err
    assert list(tmp_path.iterdir()) == []


def test_valid_argument_syntax_remains_compatible() -> None:
    parser = cli_module.build_parser()
    assert parser.parse_args(["fetch", "--start", "2026-09-25"]).start.isoformat() == (
        "2026-09-25"
    )
    assert parser.parse_args(["cost-stress", "--costs=5,10"]).costs == (5.0, 10.0)
    assert parser.parse_args(["parameter-test", "--shorts=10,20"]).shorts == (10, 20)
