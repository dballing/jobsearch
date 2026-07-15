#!/bin/bash
# Regenerate docs/screenshot.png (embedded in the README) from the committed golden mock,
# tests/snapshots/mock_screenshot.html, using headless Chrome.
#
# The mock is app-generated from the sample fixture (see tests/test_snapshots.py). Workflow
# after an intentional UI change:
#   1. UPDATE_SNAPSHOTS=1 ./run_tests.sh   # rewrite the mock (+ other goldens)
#   2. ./make_screenshot.sh                # re-capture the PNG from the mock
#   3. commit both
#
# Chrome path is overridable: CHROME=/path/to/chrome ./make_screenshot.sh
# Dimensions match the existing screenshot (1400x860, scale 1) so the README layout is stable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
MOCK="$SCRIPT_DIR/tests/snapshots/mock_screenshot.html"
OUT="$SCRIPT_DIR/docs/screenshot.png"

[ -x "$CHROME" ] || { echo "Chrome not found at '$CHROME'. Set CHROME=/path/to/chrome." >&2; exit 1; }
[ -f "$MOCK" ]   || { echo "Mock not found: $MOCK. Run UPDATE_SNAPSHOTS=1 ./run_tests.sh first." >&2; exit 1; }

# Height must fit the whole fixture table or the last rows (incl. the hotlisted one) get
# clipped — Chrome captures the window, not the full page. 1120 fits the current 16-row
# fixture with a small margin; bump it if you add rows to the sample data.
# --virtual-time-budget lets the CDN CSS/fonts (Bootstrap + icons) load before capture;
# --force-device-scale-factor=1 keeps the output at exactly the window size (no retina 2x).
"$CHROME" \
  --headless --disable-gpu --hide-scrollbars --no-sandbox \
  --force-device-scale-factor=1 --window-size=1400,1120 \
  --virtual-time-budget=10000 --default-background-color=FFFFFFFF \
  --screenshot="$OUT" "file://$MOCK" >/dev/null 2>&1

echo "Wrote $OUT"
