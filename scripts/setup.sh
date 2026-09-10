#!/usr/bin/env sh
set -eu

export PYTHONUTF8=1

SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -P "$SCRIPT_DIR/.." && pwd)
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

python_is_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)'
}

find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && python_is_supported "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

cd "$PROJECT_ROOT"

if [ -x "$VENV_PYTHON" ]; then
    if ! python_is_supported "$VENV_PYTHON"; then
        printf '%s\n' "Existing .venv does not use Python 3.11, 3.12, or 3.13." >&2
        printf '%s\n' "Remove .venv and run ./run_demo.sh again." >&2
        exit 1
    fi
else
    if ! SYSTEM_PYTHON=$(find_python); then
        printf '%s\n' "Python 3.11-3.13 was not found. Install Python 3.12 and run again." >&2
        exit 1
    fi
    "$SYSTEM_PYTHON" -m venv .venv
fi

"$VENV_PYTHON" -m pip install pip==26.2.1 setuptools==80.9.0 wheel==0.45.1
"$VENV_PYTHON" -m pip install -r requirements-lock.txt
"$VENV_PYTHON" -m pip install --no-build-isolation --no-deps -e .

printf '%s\n' "Environment setup completed."
