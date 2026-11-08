"""Own output handles independently of the PowerShell wrapper's lifetime."""

from __future__ import annotations

import faulthandler
import io
import os
import re
import runpy
import sys
from pathlib import Path

from finance_lab.paper_process_evidence import _safe

_ORIGINAL_STREAMS = (sys.stdout, sys.stderr)


def main() -> None:
    launch = sys.argv[sys.argv.index("--launch-id") + 1]
    portfolio = sys.argv[sys.argv.index("--portfolio") + 1]
    if not re.fullmatch(r"[a-f0-9]{32}", launch) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", portfolio
    ):
        raise ValueError("Invalid launch identity")
    directory = _safe(
        Path(os.environ["FINANCE_LAB_ROOT"]) / "outputs/paper_process_runs" / portfolio / launch
    )
    # Existing unique launcher directory only. Exclusive files prevent accidental
    # reuse; direct descriptors keep receiving output after the wrapper exits.
    for name, descriptor in (("stdout.log", 1), ("stderr.log", 2)):
        target = _safe(directory / name)
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        try:
            if handle != descriptor:
                os.dup2(handle, descriptor)
        finally:
            if handle != descriptor:
                os.close(handle)
        # Windows console streams retain their old HANDLE; replace the Python
        # stream as well as its CRT descriptor with an unbuffered file stream.
        stream = io.TextIOWrapper(
            io.FileIO(descriptor, "wb", closefd=False),
            encoding="utf-8",
            errors="backslashreplace",
            write_through=True,
        )
        if descriptor == 1:
            sys.stdout = stream
            sys.__stdout__ = stream  # type: ignore[misc]  # Replace Windows console handle.
        else:
            sys.stderr = stream
            sys.__stderr__ = stream  # type: ignore[misc]  # Preserve fallback error output.
    faulthandler.enable(file=sys.stderr, all_threads=True)
    runpy.run_module("finance_lab.cli", run_name="__main__")


if __name__ == "__main__":
    main()
