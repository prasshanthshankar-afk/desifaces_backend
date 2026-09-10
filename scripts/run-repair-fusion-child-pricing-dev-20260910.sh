#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
SCRIPT_COMMIT="f6cdf343a9a6a5d43a0ecd07ab3cbdef2c78c0d8"
SCRIPT_PATH="scripts/repair-certify-fusion-child-pricing-dev-20260910.py"
RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"
TMP="$(mktemp /tmp/desifaces-fusion-child-pricing-repair-XXXXXX.py)"

cleanup(){ rm -f "$TMP"; }
trap cleanup EXIT
fail(){ echo "FAIL: $*" >&2; exit 2; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

echo "ENVIRONMENT=DEV_ONLY"
echo "PRODUCTION_TOUCH=FORBIDDEN"
echo "REPAIR_SOURCE=$SCRIPT_COMMIT"
echo "RUNTIME_SCOPE=DIRECTOR_API+DIRECTOR_WORKER+DEV_WEB"
echo "FUSION_RESTART=FORBIDDEN"
echo "FACE_AUDIO_EXTENSION_RESTART=FORBIDDEN"
echo "DB_REDIS_RESTART=FORBIDDEN"
echo "PROVIDER_GENERATION=NONE"
echo "LAUNCH_MODE=IMMUTABLE_PINNED_SCRIPT_SYNTAX_GATED"

curl -fsSL "$RAW_BASE/$SCRIPT_COMMIT/$SCRIPT_PATH" -o "$TMP"
python3 -m py_compile "$TMP"
echo "REPAIR_SCRIPT_SYNTAX_GATE=PASS"
python3 "$TMP"
