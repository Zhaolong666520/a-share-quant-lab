"""Real Windows directory junctions in retained synthetic fixtures only."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_paper_operational_status import fingerprint

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows directory junction required")


def junction(link: Path, target: Path) -> None:
    executable = shutil.which("powershell.exe")
    assert executable is not None, "Windows PowerShell required; do not hide setup failure"

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    command = (
        "$ErrorActionPreference='Stop';New-Item -ItemType Junction -Path "
        + quote(link)
        + " -Target "
        + quote(target)
        + " | Out-Null"
    )
    created = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        timeout=20,
    )
    assert created.returncode == 0, created.stderr
    assert link.lstat().st_file_attributes & 0x400


@pytest.mark.parametrize("ancestor", [False, True])
def test_junction_root_or_ancestor_is_rejected_before_store_open(
    tmp_path, monkeypatch, ancestor
):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    from finance_lab.paper_store import PaperStore

    target = tmp_path / "retained-target"
    target.mkdir()
    (target / "private.json").write_text("private-junction-token", encoding="utf-8")
    child = target / "child"
    child.mkdir()
    before = fingerprint(target)
    link = tmp_path / "retained-junction"
    junction(link, target)
    store_calls = []

    def forbidden_store(*args, **kwargs):
        store_calls.append("enter")
        raise AssertionError("A junction root must fail before opening any store")

    monkeypatch.setattr(PaperStore, "__enter__", forbidden_store)
    result = module.inspect_paper_runtime_status(
        link / "child" if ancestor else link,
        "default", "2026-10-19", query_scheduler=False,
    )
    assert result["command_exit_code"] == 2
    assert result["diagnostics"] == [
        {"code": "runtime_request_invalid", "section": "request", "severity": "attention"}
    ]
    assert fingerprint(target) == before
    assert store_calls == [], "Junction rejection must precede every store entry attempt"


def test_junction_data_directory_is_unreadable_without_opening_target(tmp_path, monkeypatch):
    module = importlib.import_module("finance_lab.paper_runtime_status")
    from finance_lab.paper_store import PaperStore

    root = tmp_path / "ordinary-root"
    target = tmp_path / "retained-data-target"
    root.mkdir()
    target.mkdir()
    (target / "market_update_state.json").write_text("private-junction-token", encoding="utf-8")
    before = fingerprint(target)
    link = root / "data"
    junction(link, target)
    original_open = Path.open
    attempted = []
    store_calls = []

    def guarded_open(path, *args, **kwargs):
        if path.is_relative_to(link) or path.is_relative_to(target):
            attempted.append(str(path))
            raise AssertionError("Do not read through a data junction or its target")
        return original_open(path, *args, **kwargs)

    def forbidden_store(*args, **kwargs):
        store_calls.append("enter")
        raise AssertionError("Do not open a database through a data junction")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        patch.setattr(PaperStore, "__enter__", forbidden_store)
        result = module.inspect_paper_runtime_status(
            root, "default", "2026-10-19", query_scheduler=False,
        )
    assert result["command_exit_code"] == 1
    assert result["recorded_status"]["market"]["state"] == "unavailable"
    assert result["recorded_status"]["market"]["reason"] == "market_evidence_invalid"
    assert not attempted
    assert fingerprint(target) == before
    assert store_calls == [], "Junction rejection must precede every store entry attempt"
