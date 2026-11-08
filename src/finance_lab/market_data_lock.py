"""Cooperative, fail-fast locks for one project's market data operations."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from finance_lab.paper_lock import PaperLockError, paper_run_lock

_owners: dict[tuple[int, str], int] = {}
_owner_guard = threading.RLock()
_BUSY_MESSAGE = "已有行情数据任务正在运行，请稍后重试；不要删除锁文件。"


class MarketDataLockError(RuntimeError):
    """Another process or thread owns this project's market data lock."""


def _forget_owner(key: tuple[int, str]) -> None:
    with _owner_guard:
        _owners.pop(key)


@contextmanager
def market_data_lock(data_directory: Path) -> Iterator[None]:
    """Exclude competing callers while allowing nesting in the owning thread."""
    path = (data_directory / "market_data.lock").resolve()
    key = (os.getpid(), os.path.normcase(str(path)))
    owner = threading.get_ident()
    with ExitStack() as cleanup:
        with _owner_guard:
            current_owner = _owners.get(key)
            if current_owner is not None and current_owner != owner:
                raise MarketDataLockError(_BUSY_MESSAGE)
            if current_owner is None:
                try:
                    cleanup.enter_context(paper_run_lock(path))
                except PaperLockError as exc:
                    raise MarketDataLockError(_BUSY_MESSAGE) from exc
                _owners[key] = owner
                # Only the outer scope releases ownership and the OS lock.
                cleanup.callback(_forget_owner, key)
        yield
