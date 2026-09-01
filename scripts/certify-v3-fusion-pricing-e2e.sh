#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${V3_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BRANCH="${FUSION_FIX_BRANCH:-fix/v3-fusion-reusable-input-resolution-20260901}"
API="${FUSION_API_CONTAINER:-df-v3-svc-fusion}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/v3-fusion-pricing-e2e-${STAMP}.log"
PROBE="/tmp/certify-v3-fusion-pricing-e2e.py"

cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:scripts/certify-v3-fusion-pricing-e2e.py" > "$PROBE"
python3 -m py_compile "$PROBE"
docker cp "$PROBE" "$API":/tmp/certify-v3-fusion-pricing-e2e.py

if docker exec "$API" python /tmp/certify-v3-fusion-pricing-e2e.py >"$LOG" 2>&1; then
  cat "$LOG"
  echo "details=$LOG"
else
  echo "FUSION VIDEO + PRICING E2E: FAILED"
  echo "details=$LOG"
  tail -n 80 "$LOG"
  exit 1
fi
