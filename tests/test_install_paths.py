from __future__ import annotations

import os
from pathlib import Path

import pytest

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
    assert config.select_project_root() == working


def test_explicit_root_overrides_working_directory(monkeypatch, tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(selected))

    assert config.project_root() == selected
    assert config.select_project_root() == selected


def test_root_selection_preserves_relative_environment_path(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", "selected")

    assert config.select_project_root() == Path("selected")
    assert config.project_root() == tmp_path / "selected"
    assert config.get_paths().root == tmp_path / "selected"


def test_explicit_path_overrides_environment_without_expanding_user(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path / "environment"))

    assert config.select_project_root(Path("~/explicit")) == Path("~/explicit")
    assert config.get_paths(Path("~/explicit")).root == tmp_path / "~" / "explicit"


@pytest.mark.skipif(os.name != "nt", reason="Windows USERPROFILE expansion")
def test_environment_root_expands_user_before_resolution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("FINANCE_LAB_ROOT", "~/selected")

    assert config.select_project_root() == tmp_path / "selected"
    assert config.project_root() == tmp_path / "selected"


def test_source_checkout_remains_default_before_working_directory(
    monkeypatch, tmp_path: Path,
) -> None:
    source = tmp_path / "checkout"
    package_file = source / "src" / "finance_lab" / "config.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()
    (source / "pyproject.toml").touch()
    monkeypatch.delenv("FINANCE_LAB_ROOT", raising=False)
    monkeypatch.setattr(config, "__file__", str(package_file))
    monkeypatch.chdir(tmp_path)

    assert config.select_project_root() == source
    assert config.project_root() == source


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
