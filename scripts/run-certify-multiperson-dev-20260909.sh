#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
BACKEND_ROOT="/home/azureuser/workspace/desifaces-v3"
WEB_ROOT="/home/azureuser/workspace/desifaces-web"
WEB_REPO="https://github.com/prasshanthshankar-afk/desifaces_web.git"
CERT_COMMIT="323cf360eceacf6c4a1cbfa9b9b0b8b27055b3ab"
CERT_PATH="scripts/certify-multiperson-dev-20260909.py"
RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"

fail(){ echo "FAIL: $*" >&2; exit 1; }
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"
command -v git >/dev/null 2>&1 || fail "git is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
[[ -d "$BACKEND_ROOT/.git" ]] || fail "dev backend checkout missing: $BACKEND_ROOT"

echo "ENVIRONMENT=DEV_ONLY"
echo "PRODUCTION_TOUCH=FORBIDDEN"
echo "BACKEND_ROOT=$BACKEND_ROOT"

if [[ ! -d "$WEB_ROOT/.git" ]]; then
  echo "WEB_CHECKOUT=ABSENT_CREATE_DEV_CLONE"
  git clone --filter=blob:none "$WEB_REPO" "$WEB_ROOT"
else
  echo "WEB_CHECKOUT=PRESENT_UNCHANGED"
fi

[[ -f "$WEB_ROOT/web/Dockerfile" ]] || fail "web checkout incomplete: $WEB_ROOT/web/Dockerfile missing"

echo "DEV_LAUNCH_PREFLIGHT=PASS"
curl -fsSL "$RAW_BASE/$CERT_COMMIT/$CERT_PATH" | python3
