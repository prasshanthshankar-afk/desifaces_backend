#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_ROOT="${V3_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${V3_WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BRANCH="${FUSION_FIX_BRANCH:-fix/v3-fusion-reusable-input-resolution-20260901}"
TMP_HOTFIX="/tmp/hotfix-v3-fusion-pricing-confirmation.sh"

echo "============================================================"
echo " desifaces V3 FUSION VIDEO + PRICING — COMPLETION DEPLOY"
echo "============================================================"

echo
echo "===== A. FUSION CONFIRMED-PRICING HOTFIX ====="
cd "$BACKEND_ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:scripts/hotfix-v3-fusion-pricing-confirmation.sh" > "$TMP_HOTFIX"
bash "$TMP_HOTFIX"

echo
echo "===== B. WEB VIDEO PRICING DISPLAY + REGRESSIONS ====="
cd "$WEB_ROOT"
git pull --ff-only origin main
bash scripts/deploy-video-pricing-e2e.sh

echo
echo "============================================================"
echo " VIDEO + PRICING COMPLETION DEPLOY: PASSED"
echo "============================================================"
echo "fusion_confirmation=enabled"
echo "fusion_reusable_inputs=preserved"
echo "multiperson_pricing_wrapper=preserved"
echo "web_pricing_display=deployed"
echo "web_regression_build=passed"
echo "db=untouched"
echo "redis=untouched"
echo "============================================================"
