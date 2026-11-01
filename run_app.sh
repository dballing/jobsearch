#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d .venv ]; then
    echo "Error: .venv not found. Follow the setup instructions in README.md before running this script." >&2
    exit 1
fi

source .venv/bin/activate

# --debug enables the auto-reloader: the server restarts automatically when a .py
# file changes, and Jinja templates reload per-request — so a code change just
# needs a save (and a browser refresh), no manual Ctrl-C / re-run. Local dev only.
# Set FLASK_NO_DEBUG=1 to run without it. Extra args ("$@") still pass through.
DEBUG_FLAG="--debug"
[ -n "${FLASK_NO_DEBUG:-}" ] && DEBUG_FLAG=""

# Default to port 5001 — macOS Control Center / AirPlay Receiver squats on Flask's
# default 5000 (returns 403). An explicit `--port N` still overrides this, since the
# CLI flag takes precedence over FLASK_RUN_PORT.
# The app modules live in lib/. The flask CLI is the entry point here, not one of our
# scripts, so nothing puts lib/ on sys.path implicitly — say it explicitly.
export PYTHONPATH="$SCRIPT_DIR/lib${PYTHONPATH:+:$PYTHONPATH}"

export FLASK_RUN_PORT="${FLASK_RUN_PORT:-5001}"
flask --app app run $DEBUG_FLAG "$@"
