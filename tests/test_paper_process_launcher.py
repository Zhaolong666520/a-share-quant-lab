"""Real Windows processes owned by these isolated fixtures; no scheduler or market calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest

from finance_lab.paper_process_status import inspect_process_evidence

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")

_CHILD = r"""
import json, os, sys, time, uuid
from datetime import date
from pathlib import Path
def audit(event,args):
    if event in {'socket.connect','socket.getaddrinfo','socket.bind'}:
        raise RuntimeError('Network forbidden in fixture')
sys.addaudithook(audit)
from finance_lab.paper_process_evidence import ProcessJournal
root=Path(os.environ['FINANCE_LAB_ROOT'])
launch=sys.argv[sys.argv.index('--launch-id')+1]
mode=os.environ['FIXTURE_MODE']
if mode == 'success':
    from finance_lab.paper_automation import run_paper_daily_automation, PaperAutomationActions
    from finance_lab.paper_models import PaperOperationResult
    def update(**kw): return {'successful_instruments':1,'failed_sources':0,'status':'success'}
    def health(**kw): return root/'health.html', {'health_status':'warning'}
    def paper(**kw): return PaperOperationResult('no-op','default',(),(),None,root/'report.html')
    result=run_paper_daily_automation(root=root,as_of_date=date(2026,11,4),launch_id=launch,
        actions=PaperAutomationActions(update,health,paper,paper))
    print('synthetic success',flush=True)
    raise SystemExit(result.exit_code)
journal=ProcessJournal(root,'default',date(2026,11,4),uuid.uuid4().hex,launch)
journal.phase('market_data_update')
print('stdout before abrupt exit',flush=True)
print('stderr before abrupt exit',file=sys.stderr,flush=True)
(root/'worker-ready.json').write_text(json.dumps(journal.worker),encoding='utf-8')
if mode == 'hard-exit': os._exit(23)
if mode == 'native-exit':
    import ctypes
    ctypes.windll.kernel32.ExitProcess(0xC0000005)
if mode == 'wait':
    deadline=time.monotonic()+20
    while time.monotonic()<deadline and not (root/'release').exists(): time.sleep(.05)
    print('stdout after wrapper exit',flush=True)
    print('stderr after wrapper exit',file=sys.stderr,flush=True)
    (root/'after-output').write_text('done')
    raise SystemExit(19)
raise RuntimeError('synthetic ordinary failure')
"""


def _fixture(tmp_path: Path, mode: str):
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    script = (repository / "scripts/run_paper_daily.ps1").read_text(encoding="utf-8")
    # Keep production process/logging code exact; suppress the existing desktop
    # notification call only in this retained fixture, so tests never display UI.
    script = script.replace("Show-PaperFailureNotification $message", "$null = $message")
    runner = scripts / "run_paper_daily.ps1"
    runner.write_text(script, encoding="utf-8")
    venv.EnvBuilder(with_pip=False, symlinks=False).create(project / ".venv")
    package = project / "fixture-src/finance_lab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from pkgutil import extend_path\n__path__ = extend_path(__path__, __name__)\n"
        '__version__="0.9.1"\n',
        encoding="utf-8",
    )
    (package / "cli.py").write_text(_CHILD, encoding="utf-8")
    if mode == "import-failure":
        (package / "ledger.py").write_text(
            "import os, sys\nsys.stderr.write('synthetic business import failure\\n')\n"
            "sys.stderr.flush()\nos._exit(31)\n",
            encoding="utf-8",
        )
    root = tmp_path / "data root"
    root.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "FIXTURE_MODE": mode,
            "PYTHONPATH": os.pathsep.join(
                [
                    str(package.parent),
                    str(repository / "src"),
                    str(Path(sys.prefix) / "Lib/site-packages"),
                ]
            ),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    args = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(runner),
        "-DataRoot",
        str(root),
        "-AsOfDate",
        "2026-11-04",
    ]
    return root, env, args


@pytest.mark.parametrize(
    ("mode", "exit_code"),
    [
        ("hard-exit", 23),
        ("native-exit", 0xC0000005),
        ("error", 1),
        ("success", 0),
        ("import-failure", 31),
    ],
)
def test_real_child_exit_streams_and_receipts(tmp_path, mode, exit_code):
    root, env, args = _fixture(tmp_path, mode)
    done = subprocess.run(
        args, env=env, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
    )
    assert done.returncode & 0xFFFFFFFF == exit_code, done.stderr.decode(errors="replace")
    directory = next((root / "outputs/paper_process_runs/default").iterdir())
    child_exit = json.loads((directory / "child-exit.json").read_text(encoding="utf-8"))
    assert child_exit["exit_code_uint32"] == exit_code
    assert child_exit["exit_code_hex"] == f"0x{exit_code:08X}"
    assert (directory / "wrapper-exit.json").exists()
    if mode == "import-failure":
        assert "synthetic business import failure" in (directory / "stderr.log").read_text()
        assert not (directory / "worker-000-started.json").exists()
    elif mode != "success":
        assert "stdout before abrupt exit" in (directory / "stdout.log").read_text()
        assert "stderr before abrupt exit" in (directory / "stderr.log").read_text()
    status = inspect_process_evidence(root, "default")
    assert status["state"] == ("completed" if mode == "success" else "failed"), status
    assert status["phase_proves_commit"] is False


def test_parent_hard_exit_preserves_live_worker_and_partial_output(tmp_path):
    root, env, args = _fixture(tmp_path, "wait")
    wrapper = subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 12
        while not (root / "worker-ready.json").exists():
            assert wrapper.poll() is None, "fixture wrapper failed before worker ready"
            assert time.monotonic() < deadline
            time.sleep(0.05)
        # Popen owns this exact wrapper handle; never kill by executable name.
        wrapper.terminate()
        wrapper.wait(timeout=5)
        status = inspect_process_evidence(root, "default")
        assert status["state"] == "wrapper_exited_worker_running", status
        directory = next((root / "outputs/paper_process_runs/default").iterdir())
        assert not (directory / "wrapper-exit.json").exists()
        assert "stdout before abrupt exit" in (directory / "stdout.log").read_text()
        (root / "release").write_text("fixture continue")
        deadline = time.monotonic() + 5
        while not (root / "after-output").exists():
            assert time.monotonic() < deadline, "worker failed to write after wrapper exit"
            time.sleep(0.05)
        assert "stdout after wrapper exit" in (directory / "stdout.log").read_text()
        assert "stderr after wrapper exit" in (directory / "stderr.log").read_text()
    finally:
        (root / "release").write_text("fixture complete")
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=5)
        # The worker has its own 20-second upper bound as an additional cleanup guarantee.
        deadline = time.monotonic() + 22
        while time.monotonic() < deadline:
            if (
                inspect_process_evidence(root, "default").get("state")
                != "wrapper_exited_worker_running"
            ):
                break
            time.sleep(0.05)
