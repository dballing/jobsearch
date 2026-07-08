#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d .venv ]; then
    echo "Error: .venv not found. Follow the setup instructions in README.md before running this script." >&2
    exit 1
fi

source .venv/bin/activate

# Tests are hermetic: conftest.py points app at a throwaway config/db (JOBSEARCH_CONFIG /
# JOBSEARCH_DB) so nothing here ever touches the real jobs.db or config.toml.
python3 -m pytest tests/ "$@"
