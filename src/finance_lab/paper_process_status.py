"""Read process receipts without treating phases as transaction commits."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_lab.paper_models import validate_portfolio_id
from finance_lab.paper_process_evidence import _decode, _safe, observe_identity


def _bounded(path: Path, limit: int) -> bytes:
    with _safe(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("size_limit")
    return raw


def _stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timezone")
    return parsed


def _names(directory: Path, limit: int) -> tuple[str, ...]:
    _safe(directory)
    result = []
    with os.scandir(directory) as entries:
        for entry in entries:
            result.append(entry.name)
            if len(result) > limit:
                raise ValueError("scan_limit")
    return tuple(sorted(result))


def inspect_process_evidence(
    root: Path, portfolio: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Read-only, bounded and conservative; never authorizes recovery or a rerun."""
    clock = now or datetime.now(UTC)
    try:
        root = _safe(root)
        portfolio = validate_portfolio_id(portfolio)
        parent = _safe(root / "outputs/paper_process_runs" / portfolio)
        if not parent.exists():
            return {"state": "missing", "scheduler_correlation": "not_verified"}
        directories = _names(parent, 512)
        if not directories:
            return {"state": "missing", "scheduler_correlation": "not_verified"}
        observed: dict[Path, str] = {}
        limits: dict[Path, int] = {}

        def read(path: Path, limit: int = 65536) -> dict[str, Any]:
            raw = _bounded(path, limit)
            fingerprint = hashlib.sha256(raw).hexdigest()
            if path in observed and observed[path] != fingerprint:
                raise ValueError("receipt_changed")
            observed[path], limits[path] = fingerprint, limit
            return _decode(raw)

        def check(payload: dict[str, Any], launch_id: str) -> datetime:
            if (
                type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1
                or payload.get("launch_id") != launch_id
                or payload.get("portfolio_id") != portfolio
                or Path(payload.get("root", "")).resolve() != root.resolve()
            ):
                raise ValueError("receipt_identity")
            stamp = _stamp(payload["recorded_at"])
            if stamp > clock:
                raise ValueError("future_receipt")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload.get("as_of_date", "")):
                raise ValueError("request_date")
            return stamp

        candidates = []
        for name in directories:
            if not re.fullmatch(r"[a-f0-9]{32}", name):
                raise ValueError("launch_directory")
            directory = _safe(parent / name)
            initial = directory / "launcher.json"
            if not initial.exists():
                initial = directory / "worker-000-started.json"
            if not initial.exists():
                # An incomplete, unidentifiable directory may be the newest launch.
                raise ValueError("launch_identity_missing")
            payload = read(initial)
            stamp = check(payload, name)
            if payload.get("kind") not in {"launcher_started", "started"}:
                raise ValueError("initial_kind")
            candidates.append((stamp, name))
        candidates.sort()
        if len(candidates) > 1 and candidates[-1][0] == candidates[-2][0]:
            raise ValueError("ambiguous_latest_launch")
        launch_id = candidates[-1][1]
        directory = parent / launch_id
        names = _names(directory, 20)
        allowed = {
            "launcher.json",
            "child.json",
            "child-exit.json",
            "wrapper-exit.json",
            "stdout.log",
            "stderr.log",
        }
        worker_names = [
            n for n in names if re.fullmatch(r"worker-\d{3}-(started|phase|archived)\.json", n)
        ]
        if set(names) - allowed - set(worker_names):
            raise ValueError("receipt_filename")
        records = {n: read(directory / n) for n in names if n.endswith(".json")}
        for value in records.values():
            check(value, launch_id)
        launcher = records.get("launcher.json")
        child = records.get("child.json")
        child_exit = records.get("child-exit.json")
        wrapper_exit = records.get("wrapper-exit.json")
        started = records.get("worker-000-started.json")
        as_of = (launcher or started or {})["as_of_date"]
        if any(v["as_of_date"] != as_of for v in records.values()):
            raise ValueError("mixed_request_date")
        for name, kind in (
            ("launcher.json", "launcher_started"),
            ("child.json", "child_started"),
            ("child-exit.json", "child_exited"),
            ("wrapper-exit.json", "wrapper_exited"),
        ):
            if name in records and records[name].get("kind") != kind:
                raise ValueError("kind_mismatch")
        if (child or child_exit or wrapper_exit) and launcher is None:
            raise ValueError("launcher_missing")
        if child_exit is not None:
            code = child_exit.get("exit_code")
            if (
                type(code) is not int
                or not -(2**31) <= code < 2**31
                or child_exit.get("exit_code_uint32") != code & 0xFFFFFFFF
                or child_exit.get("exit_code_hex") != f"0x{code & 0xFFFFFFFF:08X}"
                or child is None
                or child_exit.get("child") != child.get("child")
            ):
                raise ValueError("child_exit_identity")
        if wrapper_exit is not None:
            code = wrapper_exit.get("exit_code")
            if (
                type(code) is not int
                or not -(2**31) <= code < 2**31
                or wrapper_exit.get("status") != ("succeeded" if code == 0 else "failed")
            ):
                raise ValueError("wrapper_exit_code")
            claimed_child = wrapper_exit.get("child_exit_code")
            if child_exit is not None:
                if type(claimed_child) is not int or claimed_child != child_exit["exit_code"]:
                    raise ValueError("conflicting_exit_codes")
            elif claimed_child is not None or code == 0:
                raise ValueError("missing_child_exit")
            if code == 0 and (child_exit is None or child_exit["exit_code"] != 0):
                raise ValueError("conflicting_success")
        ordered = [
            records[n]
            for n in [
                "launcher.json",
                "child.json",
                *worker_names,
                "child-exit.json",
                "wrapper-exit.json",
            ]
            if n in records
        ]
        stamps = [_stamp(v["recorded_at"]) for v in ordered]
        if stamps != sorted(stamps):
            raise ValueError("receipt_time_order")
        phases = []
        bound: dict[str, Any] | None = None
        archive_status = None
        for number, name in enumerate(worker_names):
            record = records[name]
            if not name.startswith(f"worker-{number:03d}-") or started is None:
                raise ValueError("worker_sequence")
            if record.get("kind") != name.rsplit("-", 1)[1].removesuffix(".json"):
                raise ValueError("worker_kind")
            if record.get("attempt_id") != started.get("attempt_id") or not re.fullmatch(
                r"[a-f0-9]{32}", str(record.get("attempt_id"))
            ):
                raise ValueError("attempt_identity")
            if record["kind"] == "phase":
                phases.append(record.get("phase"))
            elif record["kind"] == "archived":
                if bound is not None or name != worker_names[-1]:
                    raise ValueError("archive_order")
                relative = record.get("archive_path")
                if not isinstance(relative, str) or not re.fullmatch(
                    r"outputs/paper_automation_runs/[A-Za-z0-9_-]+\.json", relative
                ):
                    raise ValueError("archive_path")
                archive = root / relative
                payload = read(archive, 1024 * 1024)
                if observed[archive] != record.get("archive_sha256"):
                    raise ValueError("archive_sha")
                from finance_lab.paper_automation_history import _validate

                _validate(payload, portfolio, archive.name)
                if (
                    payload.get("attempt_id") != started["attempt_id"]
                    or payload["as_of_date"] != as_of
                ):
                    raise ValueError("archive_attempt")
                bound = {"path": relative, "sha256": observed[archive]}
                archive_status = payload["status"]
        expected = ["market_data_update", "data_health", "paper_run", "paper_init"]
        if phases != expected[: len(phases)] or len(phases) > 4:
            raise ValueError("phase_order")
        if started is not None:
            if started.get("origin") != ("wrapper" if launcher else "direct"):
                raise ValueError("launch_origin")
            if launcher:
                worker = started.get("worker", {})
                expected_child = (child or {}).get("child", {})
                if not expected_child or not any(
                    expected_child.get("pid") == identity.get("pid")
                    and expected_child.get("creation_filetime") is not None
                    and expected_child.get("creation_filetime") == identity.get("creation_filetime")
                    for identity in (worker, worker.get("parent", {}))
                ):
                    raise ValueError("worker_child_identity")
        identities = {}
        if launcher:
            identities["wrapper"] = observe_identity(launcher.get("wrapper"))
        if child:
            identities["launch_process"] = observe_identity(child.get("child"))
        if started:
            identities["worker"] = observe_identity(started.get("worker"))
        dead = {"exited", "exited_pid_reused"}
        if identities.get("worker") == "running":
            state = (
                "wrapper_exited_worker_running" if identities.get("wrapper") in dead else "running"
            )
        elif "running" in identities.values():
            state = "launch_process_running"
        elif not identities or "unknown" in identities.values():
            state = "unknown"
        elif wrapper_exit and wrapper_exit["exit_code"] != 0:
            state = "failed"
        elif not started:
            state = "unknown"
        elif bound and (not launcher or (wrapper_exit is not None and child_exit is not None)):
            state = (
                "completed"
                if archive_status == "success"
                and (
                    not wrapper_exit
                    or (
                        wrapper_exit["exit_code"] == 0
                        and child_exit is not None
                        and child_exit["exit_code"] == 0
                    )
                )
                else "failed"
            )
        else:
            state = "interrupted"
        if (
            _names(parent, 512) != directories
            or _names(directory, 20) != names
            or any(
                hashlib.sha256(_bounded(p, limits[p])).hexdigest() != digest
                for p, digest in observed.items()
            )
        ):
            raise ValueError("receipts_changed")
        return {
            "state": state,
            "launch_id": launch_id,
            "attempt_id": (started or {}).get("attempt_id"),
            "as_of_date": as_of,
            "phase": phases[-1] if phases else None,
            "processes": identities,
            "archive_binding": bound,
            "archive_status": archive_status,
            "child_exit_code": (child_exit or {}).get("exit_code"),
            "wrapper_exit_code": (wrapper_exit or {}).get("exit_code"),
            "scheduler_correlation": "not_verified",
            "authorizes_recovery": False,
            "phase_proves_commit": False,
        }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        return {
            "state": "unknown",
            "reason": "process_evidence_unavailable_or_inconsistent",
            "scheduler_correlation": "not_verified",
            "authorizes_recovery": False,
        }
