#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
SCRIPT_COMMIT="87ed51a49230c2a7fc3c0ded2cde0d4a76a2565c"
SCRIPT_PATH="scripts/deploy-certify-stitch-artifact-refresh-dev-20260910.sh"
TMP="$(mktemp /tmp/desifaces-stitch-artifact-refresh.XXXXXX.sh)"

cleanup(){ rm -f "$TMP" >/dev/null 2>&1 || true; }
trap cleanup EXIT

host="$(hostname -s)"
[[ "$host" == "$EXPECTED_HOST" ]] || {
  echo "FAIL: DEV host guard expected=$EXPECTED_HOST current=$host" >&2
  exit 2
}

curl -fsSL \
  "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SCRIPT_COMMIT}/${SCRIPT_PATH}" \
  -o "$TMP"

bash -n "$TMP"
echo "STITCH_ARTIFACT_REFRESH_SCRIPT_SYNTAX_GATE=PASS"
exec bash "$TMP"
