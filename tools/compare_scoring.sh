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

source .venv/bin/activate

python3 -m tools.compare_scoring "$@"
