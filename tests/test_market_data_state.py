from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from finance_lab import pipeline, storage
from finance_lab.data_manifest import generate_dataset_manifest, read_update_summary
from finance_lab.market_data_lock import market_data_lock
from finance_lab.paper_pipeline import load_paper_market_snapshot
from tests.paper_helpers import make_paper_test_project


def test_public_payload_parser_without_paths() -> None:
    import finance_lab.market_data_state as module
    assert hasattr(module, "parse_market_update_state")
    state = module.parse_market_update_state(_state_payload())
    assert state.status == "updating"
    with pytest.raises(ValueError):
        module.parse_market_update_state({"schema_version": True})


def _install_fetch(monkeypatch: pytest.MonkeyPatch, prices: pd.DataFrame) -> None:
    monkeypatch.setattr(pipeline, "fetch_akshare", lambda *args: prices.assign(source="akshare"))
    monkeypatch.setattr(pipeline, "fetch_baostock", lambda *args: prices.assign(source="baostock"))


def _update(root: Path, prices: pd.DataFrame) -> dict[str, object]:
    return pipeline.update_market_data(date(2022, 1, 1), prices["trade_date"].max().date(), root)


def test_summary_failure_quarantines_already_published_prices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    previous_summary = (paths.outputs / "update_summary.json").read_bytes()
    updated = prices.copy()
    updated["volume"] += 1
    updated["ingested_at"] = "2030-01-01T00:00:00Z"

    def fetch(*args: object) -> pd.DataFrame:
        return updated

    def interrupted_summary(*args: object, **kwargs: object) -> None:
        raise OSError("summary interrupted")

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    monkeypatch.setattr(pipeline, "write_text_atomic", interrupted_summary)
    with pytest.raises(OSError, match="summary interrupted"):
        pipeline.update_market_data(
            date(2022, 1, 1),
            prices["trade_date"].max().date(),
            tmp_path,
        )
    actual = pd.read_parquet(paths.curated / "sh_510300.parquet")
    assert list(actual["volume"]) == list(updated["volume"])
    assert (paths.outputs / "update_summary.json").read_bytes() == previous_summary
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)


def _state_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "batch_id": "a" * 32,
        "status": "updating",
        "start": "2022-01-01",
        "end": "2022-07-15",
        "symbols": ["sh.510300"],
        "started_at": "2022-07-15T10:00:00+00:00",
        "phase": "curated",
        "completed_at": None,
        "summary_sha256": None,
        "error_type": None,
    }


@pytest.mark.parametrize(
    "operation",
    [
        "raw",
        "curated",
        "sync",
        "read",
        "manifest",
        "summary",
        "validate",
        "update",
        "paper",
    ],
)
@pytest.mark.parametrize("status", ["updating", "failed"])
def test_incomplete_batch_blocks_public_operations_even_when_caller_holds_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status: str,
) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    last = prices["trade_date"].max().date()
    payload = _state_payload()
    payload["status"] = status
    payload["error_type"] = "OSError" if status == "failed" else None
    state_path = paths.data / "market_update_state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    calls: list[str] = []

    def fetch(*args: object) -> pd.DataFrame:
        calls.append("fetch")
        return prices

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    actions: dict[str, Callable[[], object]] = {
        "raw": lambda: storage.save_raw_snapshot(prices, paths),
        "curated": lambda: storage.save_curated(prices, paths),
        "sync": lambda: storage.sync_duckdb(paths),
        "read": lambda: storage.read_curated("sh.510300", paths),
        "manifest": lambda: generate_dataset_manifest(paths, as_of_date=last),
        "summary": lambda: read_update_summary(paths),
        "validate": lambda: pipeline.validate_curated_data(tmp_path),
        "update": lambda: pipeline.update_market_data(date(2022, 1, 1), last, tmp_path),
        "paper": lambda: load_paper_market_snapshot(paths, last, 3),
    }
    with market_data_lock(paths.data):
        with pytest.raises(RuntimeError, match="行情更新"):
            actions[operation]()
    assert calls == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"status": "ok"},
        {"batch_id": "../bad"},
        {"start": "2022-99-99"},
        {"end": "2020-01-01"},
        {"symbols": ["SH.510300"]},
        {"symbols": ["sh.510300", "sh.510300"]},
        {"started_at": "2022-01-01"},
        {"phase": "unknown"},
        {"unexpected": 1},
        {"error_type": "OSError"},
        {"status": "failed"},
        {"status": "completed"},
    ],
)
def test_malformed_state_is_not_legacy(tmp_path: Path, change: dict[str, object]) -> None:
    from finance_lab.market_data_state import market_update_status

    paths, _ = make_paper_test_project(tmp_path)
    payload = {**_state_payload(), **change}
    (paths.data / "market_update_state.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)
    assert market_update_status(paths)["status"] == "invalid"


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"status":"completed","status":"updating"}'])
def test_unparseable_state_blocks_read(tmp_path: Path, content: str) -> None:
    paths, _ = make_paper_test_project(tmp_path)
    (paths.data / "market_update_state.json").write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)


def test_completed_batch_binds_summary_bytes(tmp_path: Path) -> None:
    paths, prices = make_paper_test_project(tmp_path)
    summary = paths.outputs / "update_summary.json"
    payload = {
        **_state_payload(),
        "status": "completed",
        "phase": "commit",
        "completed_at": "2022-07-15T10:00:01+00:00",
        "summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest(),
    }
    (paths.data / "market_update_state.json").write_text(json.dumps(payload), encoding="utf-8")
    assert len(storage.read_curated("sh.510300", paths)) == len(prices)
    summary.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="行情更新"):
        storage.read_curated("sh.510300", paths)


@pytest.mark.parametrize("phase", ["preparing", "raw", "database", "summary", "completed"])
def test_state_write_failure_never_opens_an_incomplete_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    from finance_lab import market_data_state as state

    paths, prices = make_paper_test_project(tmp_path)
    _install_fetch(monkeypatch, prices)
    previous = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    original = state.write_text_atomic

    def fail(path: Path, text: str, **kwargs: object) -> None:
        payload = json.loads(text)
        if payload["status"] == phase or (
            payload["status"] == "updating" and payload["phase"] == phase
        ):
            raise OSError("state write interrupted")
        original(path, text)

    monkeypatch.setattr(state, "write_text_atomic", fail)
    with pytest.raises(OSError, match="state write interrupted"):
        _update(tmp_path, prices)
    if phase == "preparing":
        assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == previous
        assert state.market_update_status(paths)["status"] == "legacy"
    else:
        diagnostic = state.market_update_status(paths)
        assert diagnostic["status"] == "failed"
        assert diagnostic["blocked"]
        with pytest.raises(state.MarketDataStateError):
            storage.save_curated(prices, paths)


@pytest.mark.parametrize("secondary_failure", [False, True])
def test_database_failure_preserves_primary_error_and_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secondary_failure: bool,
) -> None:
    from finance_lab import market_data_state as state

    paths, prices = make_paper_test_project(tmp_path)
    _install_fetch(monkeypatch, prices)
    original = state.write_text_atomic

    def failed_state(path: Path, text: str, **kwargs: object) -> None:
        if secondary_failure and json.loads(text)["status"] == "failed":
            raise OSError("secondary failure")
        original(path, text)

    def failed_database(*args: object) -> int:
        raise OSError("primary database failure")

    monkeypatch.setattr(state, "write_text_atomic", failed_state)
    monkeypatch.setattr(pipeline, "sync_duckdb", failed_database)
    with pytest.raises(OSError, match="primary database failure"):
        _update(tmp_path, prices)
    result = state.market_update_status(paths)
    assert result["status"] == ("updating" if secondary_failure else "failed")
    assert result["blocked"]
    with pytest.raises(state.MarketDataStateError):
        _update(tmp_path, prices)


def test_successful_batches_bind_summary_and_release_internal_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab import market_data_state as state

    paths, prices = make_paper_test_project(tmp_path)
    _install_fetch(monkeypatch, prices)
    _update(tmp_path, prices)
    first = state.market_update_status(paths)
    assert first["status"] == "completed" and not first["blocked"]
    _update(tmp_path, prices)
    second = state.market_update_status(paths)
    assert first["state"]["batch_id"] != second["state"]["batch_id"]
    assert len(storage.read_curated("sh.510300", paths)) == len(prices)
    (paths.outputs / "update_summary.json").unlink()
    assert state.market_update_status(paths)["reason"] == "summary_unreadable"
    with pytest.raises(state.MarketDataStateError):
        storage.save_curated(prices, paths)


def test_active_writer_cannot_read_a_half_published_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.market_data_state import MarketDataStateError

    paths, prices = make_paper_test_project(tmp_path)
    checked: list[str] = []

    def fetch(*args: object) -> pd.DataFrame:
        with pytest.raises(MarketDataStateError):
            storage.read_curated("sh.510300", paths)
        with pytest.raises(MarketDataStateError):
            _update(tmp_path, prices)
        checked.append("blocked")
        return prices

    monkeypatch.setattr(pipeline, "fetch_akshare", fetch)
    monkeypatch.setattr(pipeline, "fetch_baostock", fetch)
    _update(tmp_path, prices)
    assert checked == ["blocked", "blocked"]


def test_source_failures_are_not_washed_into_healthy_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.market_data_state import market_update_status
    from finance_lab.sources import DataSourceError

    paths, prices = make_paper_test_project(tmp_path)

    def fail(*args: object) -> pd.DataFrame:
        raise DataSourceError("upstream unavailable")

    monkeypatch.setattr(pipeline, "fetch_akshare", fail)
    monkeypatch.setattr(pipeline, "fetch_baostock", fail)
    result = _update(tmp_path, prices)
    assert result["successful_instruments"] == 0
    assert market_update_status(paths)["status"] == "completed"
    assert read_update_summary(paths).records[0].source_errors == {
        "akshare": "akshare 数据源在 unknown 阶段失败",
        "baostock": "baostock 数据源在 unknown 阶段失败",
    }
    with pytest.raises(RuntimeError, match="没有有效数据行"):
        load_paper_market_snapshot(paths, prices["trade_date"].max().date(), 3)


@pytest.mark.parametrize("status", ["legacy", "completed", "failed", "invalid"])
def test_status_cli_is_read_only_and_has_meaningful_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    from finance_lab.cli import main

    paths, prices = make_paper_test_project(tmp_path)
    monkeypatch.setenv("FINANCE_LAB_ROOT", str(tmp_path))
    _install_fetch(monkeypatch, prices)
    state_path = paths.data / "market_update_state.json"
    if status == "completed":
        _update(tmp_path, prices)
    elif status == "failed":
        state_path.write_text(
            json.dumps({**_state_payload(), "status": "failed", "error_type": "OSError"}),
            encoding="utf-8",
        )
    elif status == "invalid":
        state_path.write_text("{ secret should not be echoed", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert main(["data-update-status"]) == (1 if status in {"failed", "invalid"} else 0)
    result = capsys.readouterr()
    assert "secret" not in result.out
    assert json.loads(result.out)["status"] == status
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def _collect_process_diagnostics(
    diagnostics: Path,
    outcome: dict[str, object],
    stdout: bytes,
    stderr: bytes,
    code: str,
    state_paths: tuple[Path, ...],
    synthetic_root: Path,
    test_file: Path,
) -> dict[str, object]:
    collector_errors: list[dict[str, str]] = []
    receipt: dict[str, object] = {
        **outcome,
        "last_marker": None,
        "unparseable_trace_lines": [],
        "child_script_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "synthetic_state": {},
        "file_sha256": {},
        "collector_errors": collector_errors,
    }

    def collect(step: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:
            collector_errors.append({"step": step, "type": type(exc).__name__, "error": str(exc)})

    # Keep the actual process outcome separate from all subsequent evidence I/O.
    collect(
        "outcome_write",
        lambda: (diagnostics / "outcome.json").write_text(
            json.dumps(outcome, indent=2), encoding="utf-8"
        ),
    )
    collect("stdout_write", lambda: (diagnostics / "stdout.txt").write_bytes(stdout))
    collect("stderr_write", lambda: (diagnostics / "stderr.txt").write_bytes(stderr))

    def read_trace() -> None:
        trace_errors = []
        for line in (diagnostics / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                receipt["last_marker"] = json.loads(line)
            except json.JSONDecodeError:
                trace_errors.append(line)
        receipt["unparseable_trace_lines"] = trace_errors
        if trace_errors:
            raise ValueError("trace contains unparseable lines; raw trace retained")

    collect("trace_read", read_trace)
    state_files: dict[str, object] = {}
    receipt["synthetic_state"] = state_files

    def read_state(path: Path) -> None:
        item: dict[str, object] = {}
        state_files[str(path.relative_to(synthetic_root))] = item
        try:
            path.stat()
        except FileNotFoundError:
            item["exists"] = False
            return
        item["exists"] = True
        content = path.read_bytes()
        item.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
        if path.suffix == ".json":
            try:
                item["payload"] = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                item["parse_error"] = type(exc).__name__

    for path in state_paths:
        collect(f"state_read:{path.name}", lambda path=path: read_state(path))

    def read_identity() -> None:
        project = test_file.resolve().parents[1]
        identity_files = [
            test_file.resolve(),
            project / "requirements-lock.txt",
            *sorted((project / "src" / "finance_lab").rglob("*.py")),
        ]
        receipt["file_sha256"] = {
            str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in identity_files
        }

    collect("identity_read", read_identity)
    collect(
        "receipt_write",
        lambda: (diagnostics / "receipt.json").write_text(
            json.dumps(receipt, indent=2), encoding="utf-8"
        ),
    )
    return receipt


def _assert_process_outcome(receipt: dict[str, object], diagnostics: Path, stderr: bytes) -> None:
    context = (
        f"diagnostics={diagnostics}; last_marker={receipt['last_marker']}; "
        f"returncode={receipt['returncode']}; timed_out={receipt['timed_out']}; "
        f"collector_errors={receipt['collector_errors']}; "
        f"stderr={stderr.decode('utf-8', errors='replace')}"
    )
    assert not receipt["timed_out"], f"child timeout; {context}"
    assert receipt["returncode"] == 7, f"unexpected child exit; {context}"
    assert not receipt["collector_errors"], f"diagnostic collection failed; {context}"


@pytest.mark.parametrize(
    ("returncode", "timed_out", "expected_failure"),
    [
        (-1073741819, False, "unexpected child exit"),
        (7, True, "child timeout"),
        (7, False, "diagnostic collection failed"),
    ],
)
@pytest.mark.parametrize("outcome_write_fails", [False, True])
def test_process_outcome_survives_diagnostic_io_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    timed_out: bool,
    expected_failure: str,
    outcome_write_fails: bool,
) -> None:
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text
    original_write_text = Path.write_text

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected diagnostic I/O failure")

    def write_outcome_only(path: Path, *args: object, **kwargs: object) -> int:
        if path.name != "outcome.json" or outcome_write_fails:
            raise OSError("injected receipt write failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", fail)
    monkeypatch.setattr(Path, "read_bytes", fail)
    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(Path, "write_text", write_outcome_only)
    outcome = {"returncode": returncode, "timed_out": timed_out}
    receipt = _collect_process_diagnostics(
        tmp_path, outcome, b"", b"original stderr", "", (state,), tmp_path, Path(__file__)
    )
    if not outcome_write_fails:
        saved = json.loads(original_read_text(tmp_path / "outcome.json", encoding="utf-8"))
        assert saved == outcome
    assert receipt["returncode"] == returncode and receipt["timed_out"] == timed_out
    assert {error["step"] for error in receipt["collector_errors"]} == {
        "stdout_write", "stderr_write", "trace_read", "state_read:state.json",
        "identity_read", "receipt_write",
    } | ({"outcome_write"} if outcome_write_fails else set())
    with pytest.raises(AssertionError, match=expected_failure) as failure:
        _assert_process_outcome(receipt, tmp_path, b"original stderr")
    assert "original stderr" in str(failure.value)
    assert "injected diagnostic I/O failure" in str(failure.value)


@pytest.mark.parametrize("phase", ["curated", "database", "summary"])
def test_process_death_after_publication_is_still_blocked(tmp_path: Path, phase: str) -> None:
    from finance_lab.market_data_state import MarketDataStateError, market_update_status

    paths, _ = make_paper_test_project(tmp_path)
    diagnostics_base = Path(
        os.environ.get("FINANCE_LAB_PROCESS_DIAGNOSTICS", str(tmp_path / "process-diagnostics"))
    )
    diagnostics_base.mkdir(parents=True, exist_ok=True)
    diagnostics = Path(tempfile.mkdtemp(prefix=f"{phase}-", dir=diagnostics_base))
    code = """
import faulthandler
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import time
from datetime import date
from pathlib import Path

diagnostics = Path(sys.argv[1])
phase = sys.argv[2]
trace = (diagnostics / 'trace.jsonl').open('x', encoding='utf-8')
fault = (diagnostics / 'fault.txt').open('x', encoding='utf-8')
faulthandler.enable(file=fault, all_threads=True)

def mark(event, **details):
    trace.write(json.dumps({
        'event': event, 'phase': phase, 'pid': os.getpid(),
        'monotonic_ns': time.monotonic_ns(), **details,
    }) + '\\n')
    trace.flush()
    os.fsync(trace.fileno())

mark('child_bootstrap')
from finance_lab import pipeline
from finance_lab.config import get_paths
from finance_lab.sample import make_synthetic_daily_prices
mark('imports_completed')

native_names = {
    '_duckdb', 'duckdb.duckdb', 'pyarrow.lib',
    'numpy._core._multiarray_umath', 'numpy.core._multiarray_umath',
    'pandas._libs.interval', 'pandas._libs.tslibs.np_datetime',
}
native = {}
for name, module in sorted(tuple(sys.modules.items())):
    filename = getattr(module, '__file__', None)
    if name not in native_names or not filename:
        continue
    path = Path(filename)
    if path.suffix.lower() not in {'.pyd', '.so'}:
        continue
    native[name] = {
        'path': str(path.resolve()), 'size': path.stat().st_size,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
    }
dependencies = {}
for name in ('duckdb', 'numpy', 'pandas', 'pyarrow', 'pytest', 'pytest-cov', 'coverage'):
    try:
        dependencies[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        dependencies[name] = None
environment = {
    'schema_version': 1, 'data_kind': 'synthetic',
    'python': sys.version, 'executable': sys.executable,
    'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
    'platform': platform.platform(), 'machine': platform.machine(),
    'trace_present': sys.gettrace() is not None,
    'profile_present': sys.getprofile() is not None,
    'dependencies': dependencies, 'loaded_native_extensions': native,
    'native_extension_names_considered': sorted(native_names),
    'commit': os.environ.get('GITHUB_SHA'),
}
with (diagnostics / 'environment.json').open('x', encoding='utf-8') as stream:
    json.dump(environment, stream, indent=2)
    stream.flush()
    os.fsync(stream.fileno())
mark('environment_recorded')

prices = make_synthetic_daily_prices(periods=145)
prices['symbol'] = 'sh.510300'
prices['source'] = 'akshare'
prices['volume_unit'] = 'share'
pipeline.fetch_akshare = lambda *args: prices.copy()
pipeline.fetch_baostock = lambda *args: prices.copy()

original_sync = pipeline.sync_duckdb
def observed_sync(*args, **kwargs):
    mark('sync_duckdb_enter')
    result = original_sync(*args, **kwargs)
    mark('sync_duckdb_return')
    return result
pipeline.sync_duckdb = observed_sync

name = {'curated': 'save_curated', 'database': 'sync_duckdb',
        'summary': 'write_text_atomic'}[phase]
original = getattr(pipeline, name)
def interrupted(*args, **kwargs):
    mark(phase + '_hook_enter')
    original(*args, **kwargs)
    mark(phase + '_original_return')
    mark('exit_requested', requested_exit_code=7)
    os._exit(7)
setattr(pipeline, name, interrupted)
mark('update_enter')
pipeline.update_market_data(date(2022, 1, 1), prices['trade_date'].max().date())
mark('update_return')
"""
    (diagnostics / "child.py").write_text(code, encoding="utf-8", newline="\n")
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", code, str(diagnostics), phase],
        env={
            **os.environ,
            "FINANCE_LAB_ROOT": str(tmp_path),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()
    outcome = {
        "schema_version": 1,
        "data_kind": "synthetic",
        "phase": phase,
        "pid": process.pid,
        "returncode": process.returncode,
        "returncode_hex": f"0x{process.returncode & 0xFFFFFFFF:08X}",
        "timed_out": timed_out,
        "timeout_seconds": 20,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
    }
    receipt = _collect_process_diagnostics(
        diagnostics, outcome, stdout, stderr, code,
        (
            paths.data / "market_update_state.json",
            paths.outputs / "update_summary.json",
            paths.curated / "sh_510300.parquet",
            paths.database,
            Path(str(paths.database) + ".wal"),
        ),
        tmp_path, Path(__file__),
    )
    _assert_process_outcome(receipt, diagnostics, stderr)
    assert len(pd.read_parquet(paths.curated / "sh_510300.parquet")) == 145
    assert market_update_status(paths)["status"] == "updating"
    with pytest.raises(MarketDataStateError):
        storage.read_curated("sh.510300", paths)


def test_daily_automation_stops_at_quarantine_and_preserves_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_lab.paper_automation import run_paper_daily_automation
    from finance_lab.paper_pipeline import paper_init_portfolio

    paths, prices = make_paper_test_project(tmp_path)
    last = prices["trade_date"].max().date()
    paper_init_portfolio(as_of_date=last, root=tmp_path)
    before = paths.database.read_bytes()
    state_path = paths.data / "market_update_state.json"
    state_path.write_text(json.dumps(_state_payload()), encoding="utf-8")
    original_state = state_path.read_bytes()

    def forbidden(*args: object) -> pd.DataFrame:
        raise AssertionError("fetch must not start")

    monkeypatch.setattr(pipeline, "fetch_akshare", forbidden)
    monkeypatch.setattr(pipeline, "fetch_baostock", forbidden)
    result = run_paper_daily_automation(as_of_date=last, root=tmp_path)
    assert result.exit_code == 1
    assert result.payload["failed_step"] == "market_data_update"
    assert result.payload["error"]["type"] == "MarketDataStateError"
    assert result.payload["operation_status"] is None
    assert json.loads(result.archive_path.read_text(encoding="utf-8")) == result.payload
    assert paths.database.read_bytes() == before
    assert state_path.read_bytes() == original_state


@pytest.mark.parametrize("legacy", [False, True])
def test_paper_report_only_gates_the_legacy_curated_fallback(tmp_path: Path, legacy: bool) -> None:
    from finance_lab.market_data_state import MarketDataStateError
    from finance_lab.paper_pipeline import paper_init_portfolio
    from finance_lab.paper_report import _attribution_prices, _query_frame
    from finance_lab.paper_store import PaperStore

    paths, prices = make_paper_test_project(tmp_path)
    paper_init_portfolio(as_of_date=prices["trade_date"].max().date(), root=tmp_path)
    with PaperStore(paths.database, read_only=True) as store:
        snapshot = store.portfolio_snapshot("default")
    events = _query_frame(paths.database, "SELECT * FROM paper_events", [])
    if legacy:
        for index, row in events.iterrows():
            payload = json.loads(row["payload_json"])
            payload.pop("open_price", None)
            events.at[index, "payload_json"] = json.dumps(payload)
    assert len(_attribution_prices(paths, snapshot, events)) == 2
    before = paths.database.read_bytes()
    (paths.data / "market_update_state.json").write_text(
        json.dumps(_state_payload()),
        encoding="utf-8",
    )
    if legacy:
        with pytest.raises(MarketDataStateError):
            _attribution_prices(paths, snapshot, events)
    else:
        assert len(_attribution_prices(paths, snapshot, events)) == 2
    assert paths.database.read_bytes() == before


def test_batch_permission_is_not_reusable_after_exit_or_state_replacement(tmp_path: Path) -> None:
    from finance_lab.market_data_state import MarketDataStateError, market_update_batch

    paths, prices = make_paper_test_project(tmp_path)
    with pytest.raises(MarketDataStateError):
        with market_update_batch(
            paths,
            date(2022, 1, 1),
            prices["trade_date"].max().date(),
            ("sh.510300",),
        ) as batch:
            state_path = paths.data / "market_update_state.json"
            replacement = json.loads(state_path.read_text(encoding="utf-8"))
            replacement["batch_id"] = "b" * 32
            state_path.write_text(json.dumps(replacement), encoding="utf-8")
            batch.stage("curated")
    assert json.loads(state_path.read_text(encoding="utf-8")) == replacement
    with pytest.raises(MarketDataStateError):
        batch.stage("curated")
    with pytest.raises(MarketDataStateError):
        storage.save_curated(prices, paths)
