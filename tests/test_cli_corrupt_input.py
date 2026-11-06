"""Real offline CLI processes: valid controls, then unreadable synthetic Parquet."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
from finance_lab.storage import save_curated

COMMANDS = (
    "account",
    "backtest",
    "experiment",
    "walk-forward",
    "cost-stress",
    "parameter-test",
    "execution-test",
    "compare",
)
BAD_BYTES = b"PRIVATE_SYNTHETIC_BAD_PARQUET_ONLY_NOT_MARKET_DATA"
WORK = Path(__file__).resolve().parents[1]
BOOTSTRAP = r"""
import hashlib, json, os, pathlib, runpy, sys
def offline(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
        raise RuntimeError("Offline CLI test: network blocked")
sys.addaudithook(offline)
import finance_lab
work = pathlib.Path(os.environ["CLI_TEST_WORK"])
assert pathlib.Path(finance_lab.__file__).resolve().is_relative_to(work)
trace = {"python": sys.executable, "version": sys.version,
         "module": finance_lab.__file__, "reads": [], "network_blocked": True,
         "sources": {str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (work / "src/finance_lab").glob("*.py")}}
def profile(frame, event, arg):
    if (event == "call" and frame.f_code.co_name == "read_parquet"
            and frame.f_globals.get("__name__") == "pandas.io.parquet"):
        trace["reads"].append({"path": str(frame.f_locals.get("path")),
                               "caller": frame.f_back.f_globals.get("__name__"),
                               "function": frame.f_back.f_code.co_name})
if os.environ["CLI_TEST_TRACE_READS"] == "1":
    sys.setprofile(profile)
sys.argv = ["finance-lab", *sys.argv[1:]]
try:
    runpy.run_module("finance_lab.cli", run_name="__main__")
finally:
    sys.setprofile(None)
    pathlib.Path(os.environ["CLI_TEST_TRACE"]).write_text(
        json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
"""


def _snapshot(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
            }
    return result


def _argv(command: str) -> list[str]:
    result = [command, "--symbol", "sh.510300"]
    if command in {"experiment", "compare"}:
        result += ["--split-date", "2023-01-03"]
    if command in {"walk-forward", "cost-stress", "parameter-test", "execution-test"}:
        result += ["--first-oos-date", "2023-01-03"]
    if command == "parameter-test":
        result += ["--shorts", "10,20", "--longs", "40,60"]
    return result


def _invoke(root: Path, evidence: Path, argv: list[str], label: str) -> dict:
    scratch = evidence / label
    scratch.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "FINANCE_LAB_ROOT": str(root),
            "CLI_TEST_WORK": str(WORK),
            "CLI_TEST_TRACE": str(scratch / "trace.json"),
            "CLI_TEST_TRACE_READS": "1" if label == "corrupt" else "0",
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
    invocation = [sys.executable, "-B", "-c", BOOTSTRAP, *argv]
    completed = subprocess.run(
        invocation,
        cwd=root,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    receipt = {
        "argv": invocation,
        "cwd": str(root),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "trace": json.loads((scratch / "trace.json").read_text(encoding="utf-8")),
    }
    (scratch / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return receipt


@pytest.mark.parametrize("command", COMMANDS)
def test_corrupt_parquet_actual_cli_preserves_inputs_and_old_reports(
    tmp_path: Path, command: str
) -> None:
    root = tmp_path / "project"
    paths = get_paths(root)
    (root / "config").mkdir()
    (root / "config/instruments.json").write_text(
        json.dumps(
            [{"code": "510300", "exchange": "sh", "name": "SYNTHETIC TEST ETF", "kind": "etf"}]
        ),
        encoding="utf-8",
    )
    frame = make_synthetic_daily_prices(periods=800)
    frame["symbol"] = "sh.510300"
    frame["ingested_at"] = "2026-11-06T00:00:00+00:00"
    save_curated(frame, paths)
    (paths.raw / "synthetic-preserved.txt").write_bytes(b"SYNTHETIC RAW SENTINEL")
    evidence = (
        Path(os.environ.get("FINANCE_LAB_TEST_EVIDENCE", str(tmp_path / "evidence"))) / command
    )
    evidence.mkdir(parents=True)
    argv = _argv(command)
    valid = _invoke(root, evidence, argv, "valid")
    assert valid["returncode"] == 0, valid
    assert list(paths.outputs.glob("*.bundle.json")), "positive control must publish real reports"
    curated = paths.curated / "sh_510300.parquet"
    curated.write_bytes(BAD_BYTES)  # Only this newly generated synthetic fixture is damaged.
    before = _snapshot(root)
    bad = _invoke(root, evidence, argv, "corrupt")
    after = _snapshot(root)
    (evidence / "preservation.json").write_text(
        json.dumps(
            {
                "before": before,
                "after": after,
                "permitted_mtime_only": "data/market_data.lock",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert set(before) == set(after)
    for name, record in before.items():
        actual = dict(after[name])
        if name == "data/market_data.lock":
            actual["mtime_ns"] = record["mtime_ns"]
        assert actual == record, name
    assert not paths.database.exists()
    assert not list(root.rglob("*.wal"))
    assert any(Path(item["path"]) == curated for item in bad["trace"]["reads"])
    assert bad["returncode"] != 0
    assert bad["stderr"] == ""
    payload = json.loads(bad["stdout"])
    assert payload["ok"] is False
    assert payload["error_code"] == "unreadable_curated_data"
    assert payload["stage"] == "load_curated"
    assert payload["data_committed"] is False
    assert payload["next_action"]
    assert str(root) not in bad["stdout"] + bad["stderr"]
    assert BAD_BYTES.decode() not in bad["stdout"] + bad["stderr"]
