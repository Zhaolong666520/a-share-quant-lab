"""Unreadable files must remain distinct from missing files and domain errors."""

from pathlib import Path

import pandas as pd
import pytest

from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.curated_io import CuratedReadError
from finance_lab.data_manifest import generate_dataset_manifest
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import read_curated, save_curated


@pytest.mark.parametrize("reader", ["storage", "manifest"])
@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError, ValueError])
def test_read_failure_classification_preserves_other_errors(tmp_path, monkeypatch, reader, failure):
    paths = get_paths(tmp_path)
    save_curated(make_synthetic_daily_prices(periods=120), paths)
    original = (paths.curated / "demo_000300.parquet").read_bytes()
    error = failure("private read diagnostic")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(pd, "read_parquet", fail)
    expected = CuratedReadError if failure is PermissionError else failure
    with pytest.raises(expected) as raised:
        if reader == "storage":
            read_curated("demo.000300", paths)
        else:
            generate_dataset_manifest(paths)
    if failure is PermissionError:
        assert raised.value.__cause__ is error
        assert "private" not in str(raised.value)
    else:
        assert raised.value is error
    assert (paths.curated / "demo_000300.parquet").read_bytes() == original
    assert list(paths.outputs.iterdir()) == []
    assert not paths.database.exists()


def test_business_parameter_error_is_not_misreported_as_unreadable(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    save_curated(make_synthetic_daily_prices(periods=180), get_paths(tmp_path))
    assert main(["backtest", "--symbol", "demo.000300", "--short", "80", "--long", "60"]) != 0
    output = capsys.readouterr()
    assert "unreadable_curated_data" not in output.out + output.err
    assert output.err
    assert not list((tmp_path / "outputs").iterdir())
