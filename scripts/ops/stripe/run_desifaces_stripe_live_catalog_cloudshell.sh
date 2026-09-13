#!/usr/bin/env bash
set -Eeuo pipefail

RAW_BASE="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend"
SOURCE_REF="2e99a62f645b660ec18aea6f9cee484357444263"
SCRIPT="/tmp/provision-desifaces-stripe-live.py"

cleanup() {
  unset STRIPE_SECRET_KEY || true
  unset DF_STRIPE_LIVE_PROVISION_CONFIRM || true
}
trap cleanup EXIT

echo "============================================================"
echo " desifaces — STRIPE LIVE CATALOG RUNNER"
echo " CHILD_SHELL_ONLY=YES"
echo " GITHUB_AUTH_REQUIRED=NO"
echo " PRODUCTION_DB_TOUCH=NONE"
echo " PRODUCTION_RUNTIME_TOUCH=NONE"
echo " CUSTOMER_CHARGE=NONE"
echo "============================================================"
echo "source_ref=$SOURCE_REF"

command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is required"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required"; exit 1; }

curl -fsSL \
  "${RAW_BASE}/${SOURCE_REF}/scripts/ops/stripe/provision_desifaces_stripe_live_catalog.py" \
  -o "$SCRIPT"

python3 -m py_compile "$SCRIPT"
echo "PROVISIONER_SOURCE_GATE=PASS"

read -rsp "Stripe LIVE secret key (sk_live_...): " STRIPE_SECRET_KEY
echo
export STRIPE_SECRET_KEY

case "$STRIPE_SECRET_KEY" in
  sk_live_*) ;;
  *) echo "FAIL: not an sk_live_* key"; exit 1 ;;
esac

echo
echo "===== PLAN / READ-ONLY ====="
unset DF_STRIPE_LIVE_PROVISION_CONFIRM || true
python3 "$SCRIPT"

echo
echo "===== EXPLICIT CONFIRMATION ====="
echo "Creates/reuses Stripe LIVE Products/Prices only."
echo "Does NOT modify desifaces PROD DB/runtime and does NOT charge a customer."
read -r -p "Type PROVISION-LIVE-STRIPE to continue: " CONFIRM

if [[ "$CONFIRM" != "PROVISION-LIVE-STRIPE" ]]; then
  echo "STRIPE_LIVE_PROVISION=ABORTED"
  echo "STRIPE_MUTATION=NONE"
  exit 0
fi

echo
echo "===== APPLY LIVE CATALOG ====="
export DF_STRIPE_LIVE_PROVISION_CONFIRM=YES
python3 "$SCRIPT"

echo
echo "===== ARTIFACT CERTIFICATION ====="
test -s /tmp/desifaces-stripe-live-price-mapping.sql
test -s /tmp/desifaces-stripe-live-catalog.json

echo "mapping_sql_sha256=$(sha256sum /tmp/desifaces-stripe-live-price-mapping.sql | awk '{print $1}')"
echo "catalog_json_sha256=$(sha256sum /tmp/desifaces-stripe-live-catalog.json | awk '{print $1}')"

echo "============================================================"
echo " STRIPE LIVE CATALOG PHASE=COMPLETE"
echo "============================================================"
echo "PRODUCTION_DB_TOUCH=NONE"
echo "PRODUCTION_RUNTIME_TOUCH=NONE"
echo "CUSTOMER_CHARGE=NONE"
echo "LIVE_MAPPING_APPLIED=NO"
