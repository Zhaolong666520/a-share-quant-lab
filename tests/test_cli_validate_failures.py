"""Read-only validation must not call missing or damaged data a success."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.pipeline import validate_curated_data
from finance_lab.sample import make_synthetic_daily_prices


@pytest.mark.parametrize("temporary_only", [False, True])
def test_validate_no_published_file_fails_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    temporary_only: bool,
) -> None:
    paths = get_paths(tmp_path)
    temporary = paths.curated / ".prices.parquet.deadbeef.tmp.parquet"
    if temporary_only:
        temporary.write_bytes(b"partial publication")
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["validate"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["stage"] == "discover"
    assert report["data_committed"] is False
    assert report["issues"][0]["code"] == "no_curated_files"
    assert report["next_action"]
    if temporary_only:
        assert temporary.read_bytes() == b"partial publication"


def test_validate_corrupt_parquet_reports_file_without_raw_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = get_paths(tmp_path)
    damaged = paths.curated / "sh_000300.parquet"
    damaged.write_bytes(b"secret raw marker, not a parquet")
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["validate"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["stage"] == "read"
    assert report["data_committed"] is False
    assert report["files"][0]["issues"][0]["code"] == "unreadable_parquet"
    assert "secret raw marker" not in json.dumps(report)
    assert damaged.read_bytes() == b"secret raw marker, not a parquet"


def test_validate_missing_date_field_is_classified(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120).drop(columns=["trade_date"])
    frame.to_parquet(paths.curated / "demo_000300.parquet", index=False)
    report = validate_curated_data(tmp_path)
    assert report["status"] == "failed"
    assert report["stage"] == "validate"
    assert report["data_committed"] is False
    assert "missing_columns" in {issue["code"] for issue in report["files"][0]["issues"]}


@pytest.mark.parametrize(
    ("bad_date", "expected_code"),
    [
        ("private-token-not-a-date", "invalid_date"),
        ("2022-01-11T13:30:00", "non_daily_date"),
    ],
)
def test_validate_classifies_bad_daily_dates_without_leaking_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_date: str,
    expected_code: str,
) -> None:
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120)
    frame["trade_date"] = frame["trade_date"].dt.strftime("%Y-%m-%d")
    frame.loc[5, "trade_date"] = bad_date
    frame.to_parquet(paths.curated / "demo_000300.parquet", index=False)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert main(["validate"]) == 3
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["status"] == "failed"
    assert report["stage"] == "validate"
    assert report["data_committed"] is False
    assert expected_code in {issue["code"] for issue in report["files"][0]["issues"]}
    assert "private-token" not in output.out + output.err


def test_validate_rejects_numeric_trade_dates(tmp_path: Path) -> None:
    paths = get_paths(tmp_path)
    frame = make_synthetic_daily_prices(periods=120)
    frame["trade_date"] = 0
    frame.to_parquet(paths.curated / "demo_000300.parquet", index=False)

    report = validate_curated_data(tmp_path)

    assert report["status"] == "failed"
    assert "invalid_date" in {issue["code"] for issue in report["files"][0]["issues"]}
