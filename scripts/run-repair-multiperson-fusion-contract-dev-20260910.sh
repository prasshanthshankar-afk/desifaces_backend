#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
SCRIPT_COMMIT="b735c5ce305d9ec1a93bf2b792538179dc9460ae"
SCRIPT_PATH="scripts/repair-certify-multiperson-fusion-contract-dev-20260910.py"
RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"
TMP="$(mktemp /tmp/desifaces-fusion-contract-repair-XXXXXX.py)"

cleanup(){ rm -f "$TMP"; }
trap cleanup EXIT
fail(){ echo "FAIL: $*" >&2; exit 2; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

echo "ENVIRONMENT=DEV_ONLY"
echo "PRODUCTION_TOUCH=FORBIDDEN"
echo "REPAIR_SOURCE=$SCRIPT_COMMIT"
echo "RUNTIME_SCOPE=FUSION_EXTENSION_API+DEV_WEB_ONLY"
echo "LAUNCH_MODE=IMMUTABLE_PINNED_SCRIPT_SYNTAX_GATED"

curl -fsSL "$RAW_BASE/$SCRIPT_COMMIT/$SCRIPT_PATH" -o "$TMP"
python3 -m py_compile "$TMP"
echo "REPAIR_SCRIPT_SYNTAX_GATE=PASS"
python3 "$TMP"
