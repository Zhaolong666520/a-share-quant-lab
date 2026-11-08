"""Generated-file publication leaves old outputs intact on failure."""

from __future__ import annotations

from pathlib import Path

import pytest

from finance_lab.atomic_io import write_file_atomic


def test_generated_file_writer_failure_removes_only_temporary_copy(tmp_path: Path) -> None:
    target = tmp_path / "report.png"
    target.write_bytes(b"previous")

    def interrupted_writer(temporary: Path) -> None:
        temporary.write_bytes(b"incomplete")
        raise OSError("simulated writer failure")

    with pytest.raises(OSError, match="simulated writer failure"):
        write_file_atomic(target, interrupted_writer)

    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]
