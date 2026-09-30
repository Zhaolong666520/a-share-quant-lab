import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from finance_lab import recovery
from finance_lab.cli import main
from finance_lab.market_data_lock import market_data_lock
from finance_lab.paper_lock import paper_run_lock
from finance_lab.paper_store import PaperStore
from finance_lab.recovery import RecoveryError, prepare_recovery, verify_recovery


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "源项目"
    (root / "data" / "raw").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "instruments.json").write_text("[]", encoding="utf-8")
    (root / "data" / "raw" / "evidence.bin").write_bytes(b"evidence")
    (root / "data" / "market_update_state.json").write_bytes(b"broken state")
    return root


def test_prepare_retains_evidence_and_quarantine(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "材料"
    result = prepare_recovery(source, target)
    assert result["copy_integrity"] == "verified"
    assert result["ledger_audit"]["status"] == "not_present"
    assert result["market_status"]["status"] == "invalid"
    assert result["ready_for_use"] is False
    assert (target / "snapshot/data/market_update_state.json").read_bytes() == b"broken state"
    assert (source / "data/raw/evidence.bin").read_bytes() == b"evidence"
    assert not (target / "snapshot/data/market_data.lock").exists()
    assert verify_recovery(target)["copy_integrity"] == "verified"


@pytest.mark.parametrize("change", ["modify", "extra", "missing"])
def test_verify_rejects_changed_payload(source: Path, tmp_path: Path, change: str) -> None:
    target = tmp_path / "copy"
    prepare_recovery(source, target)
    payload = target / "snapshot/data/raw/evidence.bin"
    if change == "modify":
        payload.write_bytes(b"bad")
    elif change == "missing":
        payload.unlink()
    else:
        (target / "snapshot/unlisted").write_bytes(b"extra")
    with pytest.raises(RecoveryError):
        verify_recovery(target)


def test_no_overwrite(source: Path, tmp_path: Path) -> None:
    with pytest.raises(RecoveryError):
        prepare_recovery(source, source)
    with pytest.raises(RecoveryError):
        prepare_recovery(source, source / "nested")
    with pytest.raises(RecoveryError):
        prepare_recovery(source, tmp_path)


def test_wal_refuses_without_deleting(source: Path, tmp_path: Path) -> None:
    wal = source / "data/finance_lab.duckdb.wal"
    wal.write_bytes(b"do not remove")
    target = tmp_path / "copy"
    with pytest.raises(RecoveryError):
        prepare_recovery(source, target)
    assert wal.read_bytes() == b"do not remove"
    assert not target.exists()


def test_duplicate_manifest_keys_rejected(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "copy"
    prepare_recovery(source, target)
    path = target / "recovery_manifest.json"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
    with pytest.raises(RecoveryError):
        verify_recovery(target)


def test_old_check_does_not_bypass_verification(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "copy"
    prepare_recovery(source, target)
    (target / "recovery_check.json").write_text(json.dumps({"ready_for_use": True}))
    assert verify_recovery(target)["ready_for_use"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("files", {}),
        ("directories", "data"),
        ("missing", None),
        ("bundle_id", "bad"),
        ("captured_at", "2026-09-23T00:00:00"),
        ("captured_at", None),
    ],
)
def test_invalid_manifest_fields(source: Path, tmp_path: Path, field: str, value: object) -> None:
    target = tmp_path / "copy"
    prepare_recovery(source, target)
    manifest = target / "recovery_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecoveryError):
        verify_recovery(target)
    assert json.loads((target / "recovery_check.json").read_text())["ok"] is False


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/absolute",
        "C:/outside",
        "data/../outside",
        "data/CON",
        "data/file.",
        "data/a:b",
        "data\\file",
    ],
)
def test_manifest_paths_rejected(source: Path, tmp_path: Path, name: str) -> None:
    target = tmp_path / "copy"
    prepare_recovery(source, target)
    manifest = target / "recovery_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"][0]["path"] = name
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecoveryError):
        verify_recovery(target)


@pytest.mark.parametrize("change", ["change", "add", "remove", "wal", "write_error"])
def test_mid_copy_changes_never_publish_manifest(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    target = tmp_path / "copy"
    original_copy = recovery.shutil.copyfile
    changed = False

    def copy_then_change(src: Path, dst: Path) -> None:
        nonlocal changed
        original_copy(src, dst)
        if changed:
            return
        changed = True
        if change == "change":
            (source / "data/raw/evidence.bin").write_bytes(b"changed")
        elif change == "add":
            (source / "data/new.bin").write_bytes(b"new")
        elif change == "remove":
            (source / "data/raw/evidence.bin").unlink()
        elif change == "wal":
            (source / "data/finance_lab.duckdb.wal").write_bytes(b"wal")
        else:
            raise OSError("simulated disk full")

    monkeypatch.setattr(recovery.shutil, "copyfile", copy_then_change)
    with pytest.raises(RecoveryError):
        prepare_recovery(source, target)
    assert target.exists()
    assert not (target / "recovery_manifest.json").exists()
    with pytest.raises(RecoveryError):
        verify_recovery(target)


@pytest.mark.parametrize("kind", ["paper", "market"])
def test_lock_contention_creates_no_target(source: Path, tmp_path: Path, kind: str) -> None:
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        lock = (
            paper_run_lock(source / "data/paper_trading.lock")
            if kind == "paper"
            else market_data_lock(source / "data")
        )
        with lock:
            held.set()
            release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    assert held.wait(5)
    try:
        with pytest.raises(RuntimeError):
            prepare_recovery(source, tmp_path / "copy")
        assert not (tmp_path / "copy").exists()
    finally:
        release.set()
        worker.join(5)
    assert prepare_recovery(source, tmp_path / "retry")["ok"] is True


def test_process_exit_leaves_incomplete_evidence(source: Path, tmp_path: Path) -> None:
    target = tmp_path / "copy"
    program = """
import os, sys
from pathlib import Path
from finance_lab import recovery
original = recovery.shutil.copyfile
def stop(src, dst):
    original(src, dst)
    os._exit(27)
recovery.shutil.copyfile = stop
recovery.prepare_recovery(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(source), str(target)], capture_output=True, timeout=20
    )
    assert result.returncode == 27, result.stderr.decode(errors="replace")
    assert target.exists()
    assert not (target / "recovery_manifest.json").exists()
    with pytest.raises(RecoveryError):
        verify_recovery(target)
    with pytest.raises(RecoveryError):
        prepare_recovery(source, target)
    assert prepare_recovery(source, tmp_path / "retry")["ok"] is True


def test_ledger_failure_retains_copy(source: Path, tmp_path: Path) -> None:
    database = source / "data/finance_lab.duckdb"
    database.write_bytes(b"not a database SECRET")
    result = prepare_recovery(source, tmp_path / "copy")
    assert result["copy_integrity"] == "verified"
    assert result["ledger_audit"]["status"] == "failed"
    assert result["ok"] is False
    assert "SECRET" not in json.dumps(result)
    assert database.read_bytes() == b"not a database SECRET"


def test_empty_database_ledger_roundtrip(source: Path, tmp_path: Path) -> None:
    database = source / "data/finance_lab.duckdb"
    with PaperStore(database) as store:
        store.ensure_schema()
    before = database.read_bytes()
    result = prepare_recovery(source, tmp_path / "copy")
    assert result["ledger_audit"]["status"] == "passed"
    assert database.read_bytes() == before


def test_cli_result_and_exit_codes(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(source))
    target = tmp_path / "copy"
    assert main(["data-recovery-prepare", "--destination", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ready_for_use"] is False
    assert main(["data-recovery-verify", "--bundle", str(target)]) == 0
    capsys.readouterr()
    assert main(["data-recovery-prepare", "--destination", str(target)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_missing_source_does_not_create_directories(tmp_path: Path) -> None:
    source = tmp_path / "missing"
    with pytest.raises(RecoveryError):
        prepare_recovery(source, tmp_path / "copy")
    assert not source.exists()


def test_symbolic_link_rejected(source: Path, tmp_path: Path) -> None:
    link = source / "data/link"
    outside = tmp_path / "private"
    outside.write_bytes(b"private")
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"OS does not permit symlink creation: {type(exc).__name__}")
    with pytest.raises(RecoveryError):
        prepare_recovery(source, tmp_path / "copy")
    assert outside.read_bytes() == b"private"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_cli_does_not_resolve_away_source_junction(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    link = tmp_path / "alias"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(source)], capture_output=True, timeout=10
    )
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(link))
    target = tmp_path / "copy"
    assert main(["data-recovery-prepare", "--destination", str(target)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "validation"
    assert payload["bundle_path"] == str(target)
    assert not target.exists()
    assert not (source / "data/market_data.lock").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "alias",
        "hash",
        "size",
        "unknown",
        "extra_meta",
        "missing_manifest",
        "wrong_root_type",
    ],
)
def test_manifest_integrity_edge_cases(source: Path, tmp_path: Path, mutation: str) -> None:
    target = tmp_path / "copy"
    prepare_recovery(source, target)
    manifest = target / "recovery_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if mutation in {"duplicate", "alias"}:
        extra = dict(payload["files"][0])
        if mutation == "alias":
            extra["path"] = extra["path"].upper()
        payload["files"].append(extra)
    elif mutation == "hash":
        payload["files"][0]["sha256"] = "invalid"
    elif mutation == "size":
        payload["files"][0]["size"] = True
    elif mutation == "unknown":
        payload["unexpected"] = 1
    elif mutation == "wrong_root_type":
        payload["files"][0]["path"] = "outputs"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    if mutation == "extra_meta":
        (target / "extra.json").write_bytes(b"extra")
    elif mutation == "missing_manifest":
        manifest.unlink()
    with pytest.raises(RecoveryError):
        verify_recovery(target)


def test_missing_config_and_summary_and_empty_directories(source: Path, tmp_path: Path) -> None:
    (source / "config/instruments.json").unlink()
    (source / "config").rmdir()
    (source / "data/empty").mkdir()
    (source / "outputs").mkdir()
    (source / "outputs/unrelated.txt").write_bytes(b"not included")
    target = tmp_path / "copy"
    assert prepare_recovery(source, target)["ok"] is True
    manifest = json.loads((target / "recovery_manifest.json").read_text())
    assert manifest["missing"] == ["config", "outputs/update_summary.json"]
    assert (target / "snapshot/data/empty").is_dir()
    assert not (target / "snapshot/outputs/unrelated.txt").exists()


@pytest.mark.parametrize("filename", ["recovery_manifest.json", "recovery_check.json"])
def test_metadata_write_failure_is_not_success(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    original = recovery.write_text_atomic

    def fail(path: Path, text: str) -> None:
        if path.name == filename:
            raise OSError("injected")
        original(path, text)

    monkeypatch.setattr(recovery, "write_text_atomic", fail)
    target = tmp_path / "copy"
    with pytest.raises(RecoveryError):
        prepare_recovery(source, target)
    assert not (target / filename).exists()
    assert (source / "data/raw/evidence.bin").read_bytes() == b"evidence"


def test_cli_reports_copy_failure_stage(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object) -> None:
        raise OSError("SECRET payload")

    monkeypatch.setenv("FINANCE_LAB_ROOT", str(source))
    monkeypatch.setattr(recovery.shutil, "copyfile", fail)
    target = tmp_path / "copy"
    assert main(["data-recovery-prepare", "--destination", str(target)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["stage"] == "copy"
    assert "SECRET" not in output
