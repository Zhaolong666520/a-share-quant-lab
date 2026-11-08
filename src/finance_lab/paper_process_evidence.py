"""Independent process receipts; never a market or ledger commit receipt."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_MAX_BYTES = 65536
_ID = re.compile(r"[0-9a-f]{32}")


class ProcessEvidenceWriteError(RuntimeError):
    """A process receipt failed; do not misclassify this as a source failure."""

    def __init__(self, launch_id: str, attempt_id: str, stage: str) -> None:
        self.launch_id, self.attempt_id, self.stage = launch_id, attempt_id, stage
        super().__init__(f"运行证据写入失败: {stage}; 请核对已有归档和账本收据")


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        if item.is_symlink():
            raise ValueError("process_evidence_link")
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("process_evidence_reparse")
    return path


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with _safe(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("process_evidence_duplicate_key")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("process_evidence_object")
    return value


def _load(path: Path, *, limit: int = _MAX_BYTES) -> dict[str, Any]:
    with _safe(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("process_evidence_size")
    return _decode(raw)


def process_identity(pid: int) -> dict[str, Any]:
    """Query PID plus exact Windows creation FILETIME; denied reads stay unknown."""
    result: dict[str, Any] = {"pid": pid, "creation_filetime": None, "state": "unknown"}
    if type(pid) is not int or pid <= 0 or os.name != "nt":
        return result
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER means this PID is absent, not access denied.
        result["state"] = "absent" if ctypes.get_last_error() == 87 else "unknown"
        return result
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            return result
        result["creation_filetime"] = str(
            (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
        )
        wait = kernel.WaitForSingleObject(handle, 0)
        result["state"] = "running" if wait == 258 else "exited" if wait == 0 else "unknown"
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            result["image_path"] = buffer.value
        return result
    finally:
        kernel.CloseHandle(handle)


def observe_identity(recorded: object) -> str:
    if not isinstance(recorded, dict):
        return "unknown"
    pid, creation = recorded.get("pid"), recorded.get("creation_filetime")
    if type(pid) is not int or pid <= 0 or not isinstance(creation, str) or not creation.isdigit():
        return "unknown"
    current = process_identity(pid)
    if current["state"] == "absent":
        return "exited"
    if current["creation_filetime"] is None:
        return "unknown"
    if current["creation_filetime"] != creation:
        return "exited_pid_reused"
    return str(current["state"])


class ProcessJournal:
    """One worker, exclusive immutable events; wrapper owns separate filenames."""

    def __init__(
        self, root: Path, portfolio: str, as_of: date, attempt_id: str, launch_id: str | None = None
    ) -> None:
        from finance_lab.paper_models import validate_portfolio_id

        self.root = _safe(root)
        self.portfolio = validate_portfolio_id(portfolio)
        self.launch_id = uuid.uuid4().hex if launch_id is None else launch_id
        self.attempt_id = attempt_id
        self.as_of = as_of.isoformat()
        self.sequence = 0
        if not _ID.fullmatch(self.launch_id) or not _ID.fullmatch(attempt_id):
            raise ValueError("invalid_process_run_id")
        self.directory = _safe(
            self.root / "outputs/paper_process_runs" / self.portfolio / self.launch_id
        )
        try:
            if launch_id is not None:
                launch = _load(self.directory / "launcher.json")
                if (
                    type(launch.get("schema_version")) is not int
                    or launch["schema_version"] != SCHEMA_VERSION
                    or launch.get("kind") != "launcher_started"
                    or launch.get("launch_id") != self.launch_id
                    or launch.get("portfolio_id") != self.portfolio
                    or launch.get("as_of_date") != self.as_of
                    or Path(launch.get("root", "")).resolve() != self.root.resolve()
                    or observe_identity(launch.get("wrapper")) != "running"
                ):
                    raise ValueError("launcher_context_mismatch_or_not_live")
            else:
                self.directory.mkdir(parents=True, exist_ok=False)
            _safe(self.directory)
            if not self.directory.is_dir():
                raise ValueError("process_directory_missing")
            self.worker = process_identity(os.getpid())
            self.worker["parent_pid"] = os.getppid()
            self.worker["parent"] = process_identity(os.getppid())
            if launch_id is not None:
                # The child must not enter business until its launcher has durably
                # published the actual child identity. Avoid the post-spawn write race.
                deadline = time.monotonic() + 5
                child_path = self.directory / "child.json"
                while not child_path.exists():
                    if (
                        observe_identity(launch.get("wrapper")) != "running"
                        or time.monotonic() >= deadline
                    ):
                        raise ValueError("launcher_child_handshake_missing")
                    time.sleep(0.05)
                child = _load(child_path)
                if (
                    type(child.get("schema_version")) is not int
                    or child["schema_version"] != SCHEMA_VERSION
                    or child.get("kind") != "child_started"
                    or child.get("launch_id") != self.launch_id
                    or child.get("portfolio_id") != self.portfolio
                    or child.get("as_of_date") != self.as_of
                    or Path(child.get("root", "")).resolve() != self.root.resolve()
                ):
                    raise ValueError("launcher_child_context")
                expected = child.get("child")
                if not isinstance(expected, dict) or not any(
                    expected.get("pid") == identity.get("pid")
                    and expected.get("creation_filetime") is not None
                    and expected.get("creation_filetime") == identity.get("creation_filetime")
                    for identity in (self.worker, self.worker["parent"])
                ):
                    raise ValueError("launcher_child_identity")
            module = Path(__file__).resolve()
            self.record(
                "started",
                origin="wrapper" if launch_id is not None else "direct",
                worker=self.worker,
                executable=sys.executable,
                python_version=sys.version,
                module_original_path=str(Path(__file__)),
                module_path=str(module),
                module_sha256=_sha(module),
            )
        except Exception as exc:
            if isinstance(exc, ProcessEvidenceWriteError):
                raise
            raise ProcessEvidenceWriteError(self.launch_id, attempt_id, "started") from exc

    def record(self, kind: str, **detail: Any) -> None:
        try:
            value = {
                "schema_version": SCHEMA_VERSION,
                "kind": kind,
                "launch_id": self.launch_id,
                "attempt_id": self.attempt_id,
                "portfolio_id": self.portfolio,
                "as_of_date": self.as_of,
                "root": str(self.root),
                "recorded_at": datetime.now(UTC).isoformat(),
                **detail,
            }
            path = _safe(self.directory / f"worker-{self.sequence:03d}-{kind}.json")
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > _MAX_BYTES:
                raise ValueError("process_record_size")
            with path.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self.sequence += 1
        except Exception as exc:
            raise ProcessEvidenceWriteError(self.launch_id, self.attempt_id, kind) from exc

    def phase(self, name: str) -> None:
        try:
            self.record("phase", phase=name)
        except ProcessEvidenceWriteError as exc:
            raise ProcessEvidenceWriteError(
                self.launch_id, self.attempt_id, f"phase:{name}"
            ) from exc

    def archived(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root).as_posix()
            self.record("archived", archive_path=relative, archive_sha256=_sha(path))
        except Exception as exc:
            raise ProcessEvidenceWriteError(self.launch_id, self.attempt_id, "archived") from exc
