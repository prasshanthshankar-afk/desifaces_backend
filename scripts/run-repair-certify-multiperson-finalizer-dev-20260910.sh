#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
SCRIPT_COMMIT="79dcdd359291b3cfc60674c47be0636607b0a53c"
SCRIPT_PATH="scripts/repair-certify-multiperson-finalizer-dev-20260910.sh"
TMP="$(mktemp /tmp/desifaces-finalizer-repair.XXXXXX.sh)"

cleanup() { rm -f "$TMP" >/dev/null 2>&1 || true; }
trap cleanup EXIT

host="$(hostname -s)"
if [[ "$host" != "$EXPECTED_HOST" ]]; then
  echo "FAIL: DEV host guard expected=$EXPECTED_HOST current=$host" >&2
  exit 2
fi

curl -fsSL \
  "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SCRIPT_COMMIT}/${SCRIPT_PATH}" \
  -o "$TMP"

bash -n "$TMP"
echo "FINALIZER_REPAIR_SCRIPT_SYNTAX_GATE=PASS"
bash "$TMP"
