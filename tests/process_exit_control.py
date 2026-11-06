"""One explicit Windows/database exit-mechanism control; not pytest-collected."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    batch = Path(tempfile.mkdtemp(prefix="database-terminate-process-", dir=args.output))
    project = Path(__file__).resolve().parents[1]
    files = [Path(__file__).resolve(), project / "tests" / "test_market_data_state.py",
             project / "requirements-lock.txt", project / ".github" / "workflows" / "ci.yml"]
    identity = {
        "schema_version": 1, "data_kind": "synthetic", "phase": "database",
        "termination_mode": "terminate_process", "expected_exit_code": 7,
        "python": sys.version, "executable": sys.executable, "platform": platform.platform(),
        "commit": os.environ.get("GITHUB_SHA"), "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "file_sha256": {path.relative_to(project).as_posix():
                        hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
    }
    (batch / "runner-identity.json").write_text(json.dumps(identity, indent=2), encoding="utf-8")
    previous = os.environ.get("FINANCE_LAB_PROCESS_DIAGNOSTICS")
    os.environ["FINANCE_LAB_PROCESS_DIAGNOSTICS"] = str(batch / "diagnostics")
    started = time.monotonic()
    error = None
    attempted = False
    try:
        if os.name != "nt" or sys.version_info[:2] not in {(3, 12), (3, 13)}:
            raise RuntimeError("This fixed control is only for Windows Python 3.12/3.13")
        from tests.test_market_data_state import (
            test_process_death_after_publication_is_still_blocked as run_case,
        )

        scratch_base = args.output.parent / "finance-lab-exit-control-scratch"
        scratch_base.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="synthetic-database-", dir=scratch_base))
        attempted = True
        run_case(scratch, "database", termination_mode="terminate_process")
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc),
                 "traceback": traceback.format_exc()}
    finally:
        if previous is None:
            os.environ.pop("FINANCE_LAB_PROCESS_DIAGNOSTICS", None)
        else:
            os.environ["FINANCE_LAB_PROCESS_DIAGNOSTICS"] = previous
    elapsed = time.monotonic() - started
    result = {**identity, "attempted": attempted, "status": "failed" if error else "passed",
              "elapsed_seconds": elapsed, "error": error,
              "interpretation": (
                  "This control does not replace the os._exit tests, prove the old crash fixed, "
                  "or identify its native root cause."
              )}
    (batch / "runner-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    suite = ET.Element("testsuite", name="process-exit-control", tests="1", skipped="0",
                       failures="1" if error else "0", errors="0", time=str(elapsed))
    case = ET.SubElement(suite, "testcase", classname="process_exit_control",
                         name="database_terminate_process", time=str(elapsed))
    if error:
        ET.SubElement(case, "failure", type=error["type"], message=error["message"]).text = (
            error["traceback"]
        )
    ET.ElementTree(suite).write(batch / "control-tests.xml", encoding="utf-8", xml_declaration=True)
    print(json.dumps({"status": result["status"], "evidence": str(batch), "error": error}))
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
