"""Rejected workspace links must not write through locks before validation."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from finance_lab.cli import main
from finance_lab.recovery import RecoveryError
from finance_lab.recovery_activation import verify_activation
from finance_lab.research_session import create_session, verify_session


def directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":

        def quote(path: Path) -> str:
            return "'" + str(path).replace("'", "''") + "'"

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop';New-Item -ItemType Junction -Path "
                + quote(link)
                + " -Target "
                + quote(target)
                + " | Out-Null",
            ],
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert result.returncode == 0, result.stderr
        assert link.lstat().st_file_attributes & 0x400
    else:
        link.symlink_to(target, target_is_directory=True)


def fingerprint(root: Path) -> dict:
    result = {}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            info = path.lstat()
            name = path.relative_to(root).as_posix()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                result[name] = ("link", info.st_mtime_ns)
            elif path.is_dir():
                result[name] = ("directory", info.st_mtime_ns)
                visit(path)
            else:
                result[name] = (path.read_bytes(), info.st_mtime_ns)

    visit(root)
    return result


def workspace(tmp_path: Path, operation: str) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    root.mkdir()
    if operation == "verify_session":
        snapshot = root / "snapshot"
        snapshot.mkdir()
        (root / "session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "a" * 32,
                    "created_at": "2026-11-06T00:00:00+00:00",
                    "program_version": "test",
                    "dataset_id": "b" * 64,
                    "snapshot_manifest": {},
                }
            ),
            encoding="utf-8",
        )
        return root, snapshot / "data"
    if operation == "verify_activation":
        (root / "recovery_acceptance.json").write_text("{}", encoding="utf-8")
    return root, root / "data"


def invoke(operation: str, root: Path, destination: Path) -> None:
    if operation == "create_session":
        create_session(root, destination)
    elif operation == "verify_session":
        verify_session(root)
    else:
        verify_activation(root)


@pytest.mark.parametrize("operation", ["create_session", "verify_session", "verify_activation"])
def test_data_link_rejected_without_writing_external_locks(tmp_path, operation):
    root, data = workspace(tmp_path, operation)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_bytes(b"keep")
    directory_link(data, external)
    before = fingerprint(external)
    failure = None
    try:
        invoke(operation, root, tmp_path / "destination")
    except Exception as exc:
        failure = exc
    assert fingerprint(external) == before, "Rejected input changed external files"
    assert isinstance(failure, RecoveryError), repr(failure)
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize(
    "operation,lock_name",
    [
        ("create_session", "paper_trading.lock"),
        ("create_session", "market_data.lock"),
        ("verify_activation", "paper_trading.lock"),
        ("verify_activation", "market_data.lock"),
        ("verify_session", "market_data.lock"),
    ],
)
def test_lock_link_rejected_before_touching_any_lock(tmp_path, operation, lock_name, kind):
    root, data = workspace(tmp_path, operation)
    data.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "target"
    if kind == "directory":
        target.mkdir()
        directory_link(data / lock_name, target)
    else:
        target.write_bytes(b"")
        try:
            (data / lock_name).symlink_to(target)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows file symlink privilege unavailable")
            raise
    other_lock = "market_data.lock" if lock_name == "paper_trading.lock" else "paper_trading.lock"
    (data / other_lock).write_bytes(b"existing lock")
    before_root, before_external = fingerprint(root), fingerprint(external)
    failure = None
    try:
        invoke(operation, root, tmp_path / "destination")
    except Exception as exc:
        failure = exc
    assert fingerprint(external) == before_external
    assert fingerprint(root) == before_root, "Earlier lock was touched before rejecting later link"
    assert isinstance(failure, RecoveryError), repr(failure)
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("ancestor", [False, True])
def test_session_cli_preserves_environment_root_link(tmp_path, monkeypatch, capsys, ancestor):
    real = tmp_path / "real"
    real.mkdir()
    root = real / "project" if ancestor else real
    (root / "data").mkdir(parents=True)
    link = tmp_path / "alias"
    directory_link(link, real)
    chosen = link / "project" if ancestor else link
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(chosen))
    before = fingerprint(real)
    destination = tmp_path / "session"
    assert main(["research-session-create", "--destination", str(destination)]) == 1
    response = json.loads(capsys.readouterr().out)
    assert fingerprint(real) == before, "CLI resolved away the unsafe input path"
    assert response["error_type"] == "RecoveryError"
    assert not destination.exists()
