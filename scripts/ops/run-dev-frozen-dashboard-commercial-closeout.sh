#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DASHBOARD_SHA="474e67498a6e4654a1267c6a8cdde8b2e8efaa97"
WEB_SHA="e842676a56bfc6edfc707663acfc89c0e78a4eb5"
AUDIT_SHA="f28fcb6d257a7d67945712a01d43bf5f9ae6ebda"

echo "============================================================"
echo " desifaces DEV — FROZEN WORKFLOW NARROW CLOSEOUT"
echo "============================================================"
echo "dashboard_sha=$DASHBOARD_SHA"
echo "web_sha=$WEB_SHA"
echo "audit_sha=$AUDIT_SHA"
echo "generation_workflows=FROZEN"
echo "production=UNTOUCHED"

echo
echo "===== A. DASHBOARD CANONICAL TAXONOMY ====="
gh api   "repos/prasshanthshankar-afk/desifaces_backend/contents/scripts/ops/deploy-dev-dashboard-asset-taxonomy.sh?ref=$DASHBOARD_SHA"   --jq .content | base64 -d > /tmp/df-dashboard-taxonomy.sh
chmod +x /tmp/df-dashboard-taxonomy.sh
TARGET_SHA="$DASHBOARD_SHA" bash /tmp/df-dashboard-taxonomy.sh

echo
echo "===== B. WEB DASHBOARD / SAVED WORK ONLY ====="
WEB_REPO="${WEB_REPO:-$HOME/workspace/desifaces_web}"
[[ -d "$WEB_REPO/.git" ]] || WEB_REPO="$HOME/workspace/desifaces-web"
[[ -d "$WEB_REPO/.git" ]] || { echo "FAIL: desifaces_web repo not found"; exit 2; }

gh api   "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-dev-web-target.sh?ref=$WEB_SHA"   --jq .content | base64 -d > /tmp/df-web-taxonomy-reuse.sh
chmod +x /tmp/df-web-taxonomy-reuse.sh
WEB_REPO="$WEB_REPO" TARGET_SHA="$WEB_SHA" bash /tmp/df-web-taxonomy-reuse.sh

echo
echo "===== C. ALL-STUDIO SKU / COGS E2E — READ ONLY ====="
gh api   "repos/prasshanthshankar-afk/desifaces_backend/contents/scripts/ops/certify-dev-studio-sku-cogs-e2e.sh?ref=$AUDIT_SHA"   --jq .content | base64 -d > /tmp/df-studio-sku-cogs-e2e.sh
chmod +x /tmp/df-studio-sku-cogs-e2e.sh

set +e
bash /tmp/df-studio-sku-cogs-e2e.sh 2>&1 | tee /tmp/df-studio-sku-cogs-e2e.log
AUDIT_RC=${PIPESTATUS[0]}
set -e

echo
echo "============================================================"
echo " NARROW CLOSEOUT SUMMARY"
echo "============================================================"
echo "DASHBOARD_TAXONOMY_DEPLOY=COMPLETE"
echo "WEB_TAXONOMY_REUSE_DEPLOY=COMPLETE"
echo "SKU_COGS_AUDIT_RC=$AUDIT_RC"
echo "sku_cogs_log=/tmp/df-studio-sku-cogs-e2e.log"
echo "generation_workflows=FROZEN"
echo "db_mutation_from_commercial_gate=NONE"
echo "production=UNTOUCHED"

if [[ "$AUDIT_RC" != "0" ]]; then
  echo "NARROW_CLOSEOUT=PRICING_REPAIR_REQUIRED"
  exit "$AUDIT_RC"
fi

echo "NARROW_CLOSEOUT=PASS"
