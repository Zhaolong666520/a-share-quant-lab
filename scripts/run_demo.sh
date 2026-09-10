#!/usr/bin/env sh
set -eu

export PYTHONUTF8=1

SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -P "$SCRIPT_DIR/.." && pwd)
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
REPORT_PATH="$PROJECT_ROOT/outputs/demo_000300_sma_report.html"

if [ ! -x "$VENV_PYTHON" ]; then
    "$SCRIPT_DIR/setup.sh"
elif ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)'; then
    "$SCRIPT_DIR/setup.sh"
fi

cd "$PROJECT_ROOT"
"$VENV_PYTHON" -m finance_lab.cli demo

if [ ! -f "$REPORT_PATH" ]; then
    printf '%s\n' "Demo finished without creating the expected report: $REPORT_PATH" >&2
    exit 1
fi

printf '\n%s\n' "Demo completed. Open this report in your browser:"
printf '%s\n' "$REPORT_PATH"
