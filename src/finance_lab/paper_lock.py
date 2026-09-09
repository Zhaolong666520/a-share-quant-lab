"""Cross-process locking for local paper-trading runs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class PaperLockError(RuntimeError):
    """Raised when another local paper-trading process holds the run lock."""


@contextmanager
def paper_run_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive, non-blocking lock for one paper-trading run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handle = path.open("r+b")
    acquired = False

    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _lock_non_blocking(handle)
        acquired = True
        yield
    finally:
        if acquired:
            _unlock(handle)
        handle.close()


def _lock_non_blocking(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError as exc:
            raise PaperLockError("已有模拟盘任务正在运行，请稍后再试。") from exc
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
    except OSError as exc:
        raise PaperLockError("已有模拟盘任务正在运行，请稍后再试。") from exc


def _unlock(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
