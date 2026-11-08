import hashlib
import json
import platform
from pathlib import Path

import pytest

from finance_lab.cli import main
from finance_lab.config import get_paths
from finance_lab.filename_audit import audit_curated_filenames


def inventory(path: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}


def test_real_platform_aliases_and_read_only_cli(tmp_path, monkeypatch, capsys, record_property):
    paths = get_paths(tmp_path)
    record_property("platform", platform.platform())
    record_property("python", platform.python_version())
    for name in (
        "sh_510300.parquet",
        "SH_510300.PARQUET",
        "café_510300.parquet",
        "cafe\u0301_510300.parquet",
    ):
        (paths.curated / name).write_bytes(b"filename audit must not parse content")
    before = inventory(paths.curated)
    record_property("directory_entries", len(before))
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    assert main(["filename-audit"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["content_checked"] is False
    assert result["data_committed"] is False
    assert any(item["code"] == "noncanonical_filename" for item in result["issues"])
    if "sh_510300.parquet" in before and "SH_510300.PARQUET" in before:
        assert any(item["code"] == "portable_alias_collision" for item in result["issues"])
    assert inventory(paths.curated) == before
    assert not paths.database.exists()


def test_real_platform_symlink_is_not_followed(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "sh_510300.parquet").write_bytes(b"external untouched")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Host does not permit symlink creation; Linux/macOS CI must exercise this")
    with pytest.raises(RuntimeError):
        audit_curated_filenames(alias)
    assert (external / "sh_510300.parquet").read_bytes() == b"external untouched"
