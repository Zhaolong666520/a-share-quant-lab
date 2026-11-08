"""Bounded, read-only runtime observations; log lines are not transaction receipts."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from threading import Event, Timer
from typing import Any

from finance_lab.market_data_state import parse_market_update_state
from finance_lab.operational_status_io import MAX_ARCHIVES, Observation, safe_path, strict_json

_WINDOWS = os.name == "nt"
_QUERY_TIMEOUT = 15
_QUERY_BYTES = 65536


def _log_paths(root: Path, portfolio: str) -> list[Path] | None:
    directory = root / "outputs" / "paper_automation_logs"
    if safe_path(directory, directory=True) is None:
        return None
    selected: list[Path] = []
    scanned = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.startswith(portfolio + "-"):
                continue
            scanned += 1
            if scanned > MAX_ARCHIVES:
                raise ValueError("log_scan_limit")
            match = re.fullmatch(r"([a-z0-9]+(?:-[a-z0-9]+)*)-(\d{4})-(\d{2})\.log", entry.name)
            if match is None:
                raise ValueError("log_name")
            if match[1] != portfolio:
                continue
            selected.append(directory / entry.name)
            if len(selected) > 120:
                raise ValueError("log_limit")
    return selected


class RuntimeObservation(Observation):
    """Watch exact portfolio log membership without changing the older observer."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._log_portfolio: str | None = None
        self._log_signature: object = None

    def log_paths(self, portfolio: str) -> list[Path] | None:
        self._log_portfolio = portfolio
        try:
            paths = _log_paths(self.root, portfolio)
            self._log_signature = tuple(sorted(p.name for p in paths or []))
            return paths
        except (OSError, ValueError):
            self._log_signature = "unavailable"
            raise

    def changed(self) -> set[str]:
        changed = super().changed()
        if self._log_portfolio is not None:
            try:
                paths = _log_paths(self.root, self._log_portfolio)
                signature: object = tuple(sorted(p.name for p in paths or []))
            except (OSError, ValueError):
                signature = "unavailable"
            if signature != self._log_signature:
                changed.add("runtime_log")
        return changed


def moment(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timezone")
    return parsed


def day(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date")
    return parsed


def _remember_latest(
    records: dict[str, tuple[datetime, dict[str, Any], bool]],
    kind: str,
    stamp: datetime,
    record: dict[str, Any],
) -> None:
    previous = records.get(kind)
    if previous is None or stamp > previous[0]:
        records[kind] = stamp, record, False
    elif stamp == previous[0] and record != previous[1]:
        records[kind] = stamp, previous[1], True


def _logs(root: Path, portfolio: str, observer: RuntimeObservation) -> dict[str, Any]:
    try:
        paths = observer.log_paths(portfolio)
        if not paths:
            return {"state": "missing", "latest_start": None, "latest_terminal": None}
        records: dict[str, tuple[datetime, dict[str, Any], bool]] = {}
        for path in paths:
            match = re.fullmatch(re.escape(portfolio) + r"-(\d{4})-(\d{2})\.log", path.name)
            if match is None:
                raise ValueError("log_name")
            date(int(match[1]), int(match[2]), 1)
            raw = observer.read(path, "runtime_log")
            if raw is None:
                raise ValueError("log_disappeared")
            for line in raw.decode("utf-8-sig").splitlines():
                # Embedded JSON and command output are not interpreted as log events.
                if not line.startswith("[") or "] status=" not in line:
                    continue
                match = re.fullmatch(r"\[([^\]]+)\] status=(.*)", line)
                if match is None:
                    raise ValueError("log_line")
                stamp, message = match.groups()
                timestamp = moment(stamp)
                if message.startswith("started"):
                    started = re.fullmatch(r"started as_of=(\S+) portfolio=(\S+)", message)
                    if started is None or started[2] != portfolio:
                        raise ValueError("log_start")
                    day(started[1])
                    _remember_latest(
                        records,
                        "start",
                        timestamp,
                        {"started_at": stamp, "as_of_date": started[1]},
                    )
                elif message == "succeeded":
                    _remember_latest(
                        records,
                        "terminal",
                        timestamp,
                        {"recorded_at": stamp, "status": "succeeded", "exit_code": 0},
                    )
                elif message.startswith("failed"):
                    failed = re.fullmatch(r"failed exit_code=(-?\d+) message=.*", message)
                    if failed is None or len(failed[1]) > 11 or int(failed[1]) == 0:
                        raise ValueError("log_failure")
                    _remember_latest(
                        records,
                        "terminal",
                        timestamp,
                        {"recorded_at": stamp, "status": "failed", "exit_code": int(failed[1])},
                    )
                # notification_warning and future non-outcome messages are not completion.
        if any(record[2] for record in records.values()):
            raise ValueError("ambiguous_log")
        return {
            "state": "available",
            "latest_start": records["start"][1] if "start" in records else None,
            "latest_terminal": records["terminal"][1] if "terminal" in records else None,
        }
    except (OSError, ValueError, TypeError, OverflowError):
        return {"state": "unavailable", "latest_start": None, "latest_terminal": None}


def read_runtime_evidence(
    root: Path, portfolio: str, observer: RuntimeObservation
) -> dict[str, Any]:
    log = _logs(root, portfolio, observer)
    marker: dict[str, Any] = {"state": "missing", "process_state": "unknown"}
    try:
        raw = observer.read(root / "data" / "market_update_state.json", "runtime_marker")
        if raw is not None:
            parsed = parse_market_update_state(strict_json(raw))
            marker.update(
                state="available",
                status=parsed.status,
                phase=parsed.phase,
                started_at=parsed.started_at,
                end=parsed.end,
                completed_at=parsed.completed_at,
            )
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        marker = {"state": "unavailable", "process_state": "unknown"}
    return {"log": log, "marker": marker}


def _kill_query(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with suppress(OSError):
            process.kill()


def _bounded_query(command: list[str]) -> bytes | None:
    """The owned fixed script creates no descendants inheriting its output pipe."""
    try:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=0x08000000 if os.name == "nt" else 0,
        ) as process:
            timer: Timer | None = None
            timer_started = False
            try:
                expired = Event()

                def expire() -> None:
                    expired.set()
                    _kill_query(process)

                timer = Timer(_QUERY_TIMEOUT, expire)
                timer.daemon = True
                timer.start()
                timer_started = True
                if process.stdout is None:
                    return None
                output = process.stdout.read(_QUERY_BYTES + 1)
                if len(output) > _QUERY_BYTES:
                    return None
                code = process.wait(timeout=_QUERY_TIMEOUT)
                return output if code == 0 and not expired.is_set() else None
            finally:
                _kill_query(process)
                if timer is not None:
                    timer.cancel()
                    if timer_started:
                        timer.join()
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None


def read_scheduler_status(task_name: str) -> dict[str, Any]:
    """Query one task using an owned, bounded process. Never start or repair a task."""
    unknown = {"status": "unknown", "reason": "query_unavailable"}
    if not _WINDOWS:
        return {"status": "unknown", "reason": "platform_unsupported"}
    executable = shutil.which("powershell.exe")
    if executable is None:
        return {"status": "unknown", "reason": "powershell_missing"}
    script = Path(__file__).parent / "resources" / "paper_scheduler_status.ps1"
    try:
        output = _bounded_query(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-TaskName",
                task_name,
            ],
        )
        if output is None or len(output) > _QUERY_BYTES:
            return unknown
        payload = strict_json(output.removeprefix(b"\xef\xbb\xbf"))
        return payload if isinstance(payload, dict) else unknown
    except (OSError, ValueError, subprocess.TimeoutExpired, RecursionError):
        return unknown
