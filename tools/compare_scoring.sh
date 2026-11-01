#!/bin/bash
set -euo pipefail

# This wrapper lives in tools/, but everything it touches (.venv, config.toml, jobs.db) is
# keyed to the REPO ROOT, so resolve that — one level up — rather than the script's own dir.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -d .venv ]; then
    echo "Error: .venv not found. Follow the setup instructions in README.md before running this script." >&2
    exit 1
fi

# The modules these tools import (config, viability, ai_config…) live in lib/, and running
# a script in tools/ puts tools/ on sys.path, not lib/ — so point Python at it.
export PYTHONPATH="$REPO_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"

source .venv/bin/activate

python3 tools/compare_scoring.py "$@"
