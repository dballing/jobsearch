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
flask --app app run $DEBUG_FLAG "$@"
