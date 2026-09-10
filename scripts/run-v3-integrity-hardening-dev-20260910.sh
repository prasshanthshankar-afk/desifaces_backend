#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
HARDENING_COMMIT="738dc2286745001669aae5c3b98582c6eb92a6e7"
HARDENING_PATH="scripts/harden-certify-v3-dev-20260910.py"
RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"

fail(){ echo "FAIL: $*" >&2; exit 2; }
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

echo "ENVIRONMENT=DEV_ONLY"
echo "PRODUCTION_TOUCH=FORBIDDEN"
echo "HARDENING_SOURCE=$HARDENING_COMMIT"
echo "LAUNCH_MODE=IMMUTABLE_PINNED_SCRIPT"

curl -fsSL "$RAW_BASE/$HARDENING_COMMIT/$HARDENING_PATH" | python3
