from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_second_process_is_rejected_while_paper_run_is_active(tmp_path: Path) -> None:
    lock_path = tmp_path / "paper_trading.lock"
    project_root = Path(__file__).resolve().parents[1]
    child_code = f"""
from pathlib import Path
from finance_lab.paper_lock import PaperLockError, paper_run_lock

try:
    with paper_run_lock(Path({str(lock_path)!r})):
        pass
except PaperLockError as exc:
    print(str(exc))
    raise SystemExit(2)
"""
    environment = {**os.environ, "PYTHONPATH": str(project_root / "src")}

    holder_code = f"""
import subprocess
import sys
from pathlib import Path
from finance_lab.paper_lock import paper_run_lock

with paper_run_lock(Path({str(lock_path)!r})):
    completed = subprocess.run(
        [sys.executable, "-c", {child_code!r}],
        capture_output=True,
        text=True,
        check=False,
    )
print(completed.returncode)
print(completed.stdout, end="")
"""

    result = subprocess.run(
        [sys.executable, "-c", holder_code],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "2"
    assert "已有模拟盘任务正在运行" in result.stdout
