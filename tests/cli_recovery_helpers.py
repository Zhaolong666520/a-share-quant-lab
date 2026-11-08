"""Offline, observable real CLI processes and non-following file inventories."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

WORK = Path(__file__).resolve().parents[1]
SECRET = "PRIVATE_SYNTHETIC_IO_DIAGNOSTIC"
BOOTSTRAP = r"""
import hashlib, json, os, pathlib, runpy, sys
def offline(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
        raise RuntimeError("Offline recovery CLI validation: network blocked")
sys.addaudithook(offline)
import finance_lab
from finance_lab import atomic_io
work = pathlib.Path(os.environ["CLI_TEST_WORK"])
assert pathlib.Path(finance_lab.__file__).resolve().is_relative_to(work)
trace = {"python": sys.executable, "version": sys.version,
         "module": finance_lab.__file__, "events": [], "replace_failures": [],
         "sources": {p.relative_to(work).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (work / "src/finance_lab").glob("*.py")}}
original_replace = atomic_io.os.replace
fault = os.environ.get("CLI_TEST_REPLACE_FAILURE", "")
def replace(source, destination, *args, **kwargs):
    if fault and pathlib.Path(destination) == pathlib.Path(fault):
        trace["replace_failures"].append({"source": str(source), "destination": str(destination)})
        raise OSError("PRIVATE_SYNTHETIC_IO_DIAGNOSTIC")
    return original_replace(source, destination, *args, **kwargs)
atomic_io.os.replace = replace
watched = {"create_session", "verify_session", "activate_candidate", "verify_activation",
           "verify_candidate", "generate_dataset_manifest", "verify_recovery",
           "paper_run_lock", "market_data_lock"}
def profile(frame, event, arg):
    if (event in {"call", "return"} and frame.f_code.co_name in watched
            and frame.f_globals.get("__name__", "").startswith("finance_lab.")):
        record = {"function": frame.f_code.co_name, "event": event}
        if event == "return":
            record["returned_type"] = type(arg).__name__
            if isinstance(arg, dict):
                record["candidate_id"] = arg.get("candidate_id")
            elif hasattr(arg, "dataset_id"):
                record["dataset_id"] = arg.dataset_id
        trace["events"].append(record)
sys.setprofile(profile)
sys.argv = ["finance-lab", *sys.argv[1:]]
try:
    runpy.run_module("finance_lab.cli", run_name="__main__")
finally:
    sys.setprofile(None)
    pathlib.Path(os.environ["CLI_TEST_TRACE"]).write_text(
        json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
"""


def fingerprint(root: Path) -> dict:
    result = {}

    def visit(folder):
        for path in sorted(folder.iterdir()):
            info = path.lstat()
            name = path.relative_to(root).as_posix()
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                result[name] = {"kind": "link", "mtime_ns": info.st_mtime_ns}
            elif path.is_dir():
                result[name] = {"kind": "directory"}
                visit(path)
            else:
                result[name] = {
                    "kind": "file",
                    "size": info.st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mtime_ns": info.st_mtime_ns,
                }

    visit(root)
    return result


def assert_preserved(before: dict, after: dict, allow_lock_time: bool = False) -> None:
    assert set(before) == set(after)
    for name, value in before.items():
        actual = dict(after[name])
        if allow_lock_time and name.endswith(
            ("/data/market_data.lock", "/data/paper_trading.lock")
        ):
            actual["mtime_ns"] = value["mtime_ns"]
        assert actual == value, name


def invoke(
    root: Path, evidence: Path, label: str, argv: list[str], *, fault: Path | None = None
) -> dict:
    scratch = evidence / label
    scratch.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "FINANCE_LAB_ROOT": str(root),
            "CLI_TEST_WORK": str(WORK),
            "CLI_TEST_TRACE": str(scratch / "trace.json"),
            "CLI_TEST_REPLACE_FAILURE": str(fault) if fault else "",
            "PYTHONPATH": str(WORK / "src") + os.pathsep + str(WORK),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "MPLCONFIGDIR": str(scratch),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
    )
    command = [sys.executable, "-B", "-c", BOOTSTRAP, *argv]
    result = subprocess.run(
        command,
        cwd=evidence,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    receipt = {
        "argv": command,
        "root": str(root),
        "fault": str(fault) if fault else None,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "trace": json.loads((scratch / "trace.json").read_text(encoding="utf-8")),
    }
    (scratch / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return receipt
