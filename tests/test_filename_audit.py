"""Portable filename risks must be reported without touching curated data."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from finance_lab import cli
from finance_lab.config import get_paths
from finance_lab.filename_audit import audit_curated_filenames, audit_filename_names
from finance_lab.market_data_lock import market_data_lock


def _codes(result: dict[str, object]) -> set[str]:
    return {issue["code"] for issue in result["issues"]}  # type: ignore[index]


def test_canonical_filename_passes() -> None:
    result = audit_filename_names(["sh_510300.parquet", "sz_159919.parquet"])
    assert result["ok"] is True
    assert result["candidate_count"] == 2
    assert result["issues"] == []


def test_empty_directory_is_not_claimed_as_data_validation(tmp_path: Path) -> None:
    result = audit_curated_filenames(tmp_path)
    assert result["ok"] is True
    assert result["candidate_count"] == 0
    assert result["content_checked"] is False


@pytest.mark.parametrize(
    "name", ["SH_510300.parquet", "sh_510300.PARQUET", ".sh_510300.parquet"]
)
def test_noncanonical_candidate_is_reported(name: str) -> None:
    result = audit_filename_names([name])
    assert result["ok"] is False
    assert "noncanonical_filename" in _codes(result)


@pytest.mark.parametrize("name", ["sh_510300.parquet.", "sh_510300.parquet "])
def test_windows_trailing_alias_is_reported(name: str) -> None:
    result = audit_filename_names(["sh_510300.parquet", name])
    assert "portable_alias_collision" in _codes(result)
    assert "noncanonical_filename" in _codes(result)


def test_unicode_normalization_alias_is_reported() -> None:
    result = audit_filename_names(["café_510300.parquet", "cafe\u0301_510300.parquet"])
    assert "portable_alias_collision" in _codes(result)


def test_casefold_alias_is_reported() -> None:
    result = audit_filename_names(["sh_510300.parquet", "SH_510300.PARQUET"])
    assert "portable_alias_collision" in _codes(result)


def test_legacy_staging_is_only_counted() -> None:
    result = audit_filename_names([".sh_510300.tmp.parquet"])
    assert result["ignored_staging_count"] == 1
    assert result["candidate_count"] == 0
    assert result["issues"] == []


def test_other_non_parquet_files_are_not_reported() -> None:
    result = audit_filename_names(["readme.txt", ".sh_510300.parquet.abc.tmp"])
    assert result["candidate_count"] == 0
    assert result["issues"] == []


def test_candidate_directory_is_reported(tmp_path: Path) -> None:
    (tmp_path / "sh_510300.parquet").mkdir()
    result = audit_curated_filenames(tmp_path)
    assert "not_regular_file" in _codes(result)


def test_candidate_symlink_is_reported_without_following_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = tmp_path / "sh_510300.parquet"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation unavailable on this Windows host")
    result = audit_curated_filenames(tmp_path)
    assert "symlink_candidate" in _codes(result)
    assert outside.read_text(encoding="utf-8") == "private"


def test_cli_passes_with_one_json_and_no_file_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = get_paths(tmp_path)
    target = paths.curated / "sh_510300.parquet"
    target.write_bytes(b"not read by the filename audit")
    before = target.read_bytes()
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert cli.main(["filename-audit"]) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is True
    assert payload["candidate_count"] == 1
    assert payload["content_checked"] is False
    assert output.err == ""
    assert target.read_bytes() == before
    assert not list(paths.outputs.iterdir())


def test_cli_fails_on_alias_without_changing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = get_paths(tmp_path)
    target = paths.curated / "SH_510300.parquet"
    target.write_bytes(b"original")
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    assert cli.main(["filename-audit"]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert "noncanonical_filename" in _codes(payload)
    assert target.read_bytes() == b"original"


def test_busy_cli_fails_before_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = get_paths(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "audit_curated_filenames", lambda *_args: pytest.fail("scan started"))
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
        result = cli.main(["filename-audit"])
    finally:
        release.set()
        holder.join(timeout=5)
    assert result != 0
    assert json.loads(capsys.readouterr().out)["error_code"] == "market_snapshot_busy"


def test_cli_redacts_enumeration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))

    def fail(_directory: Path) -> None:
        raise OSError("C:/private/secret-file.parquet")

    monkeypatch.setattr(cli, "audit_curated_filenames", fail)
    assert cli.main(["filename-audit"]) != 0
    output = capsys.readouterr()
    assert json.loads(output.out)["error_code"] == "filename_audit_failed"
    assert "private" not in output.out + output.err
