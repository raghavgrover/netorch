#!/bin/bash
# =============================================================================
# run_tests.sh — Run the netorch Phase 2 integration test suite
#
# Run from the /opt/netorch directory:
#   bash scripts/run_tests.sh
#
# Options:
#   -v    verbose output
#   -k    filter tests by name (passed through to pytest)
#
# Example:
#   bash scripts/run_tests.sh -v
#   bash scripts/run_tests.sh -k test_retry
# =============================================================================
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$INSTALL_DIR/venv"

cd "$INSTALL_DIR"

if [[ ! -f "$VENV/bin/pytest" ]]; then
    echo "[tests] Installing test dependencies..."
    "$VENV/bin/pip" install --quiet pytest pytest-asyncio httpx
fi

echo "[tests] Running netorch test suite from $INSTALL_DIR"
echo ""

PYTHONPATH="$INSTALL_DIR" "$VENV/bin/pytest" \
    tests/ \
    --tb=short \
    --no-header \
    -q \
    "$@"
