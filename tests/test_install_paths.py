from __future__ import annotations

from pathlib import Path

import finance_lab.config as config
from finance_lab.cli import main


def test_default_root_for_installed_package_is_working_directory(
    monkeypatch, tmp_path: Path
) -> None:
    package_file = tmp_path / "venv" / "Lib" / "site-packages" / "finance_lab" / "config.py"
    working = tmp_path / "work"
    working.mkdir()
    monkeypatch.delenv("FINANCE_LAB_ROOT", raising=False)
    monkeypatch.setattr(config, "__file__", str(package_file))
    monkeypatch.chdir(working)

    assert config.project_root() == working


def test_explicit_root_overrides_working_directory(monkeypatch, tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(selected))

    assert config.project_root() == selected


def test_init_creates_example_without_overwriting_existing_configuration(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["init"]) == 0
    target = tmp_path / "config" / "instruments.json"
    assert target.is_file()
    assert len(config.load_instruments(tmp_path)) == 2
    target.write_text("custom", encoding="utf-8")

    assert main(["init"]) == 0
    assert target.read_text(encoding="utf-8") == "custom"
