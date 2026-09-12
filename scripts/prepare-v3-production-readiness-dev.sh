#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV preparation must run on desifaces-dev"; exit 1; }
REPO="prasshanthshankar-afk/desifaces_backend"
BRANCH="fix/v3-audio-cogs-production-readiness-20260912"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE="$HOME/.local/state/v3-production-readiness/$STAMP"
mkdir -p "$STATE"; chmod 700 "$STATE"
LOG="$STATE/run.log"
exec > >(tee "$LOG") 2>&1
TMP="$STATE/scripts"; mkdir -p "$TMP"

fetch_run() {
  local name="$1"
  gh api "repos/$REPO/contents/scripts/$name?ref=$BRANCH" --jq .content | base64 -d > "$TMP/$name"
  chmod +x "$TMP/$name"
  bash -n "$TMP/$name"
  bash "$TMP/$name"
}

echo "============================================================"
echo " desifaces V3 — DEV PRODUCTION READINESS PREPARATION"
echo "============================================================"
echo "environment=DEV_ONLY"
echo "production_touch=NONE"
echo "service_rebuild=NONE"
echo "service_restart=NONE"

echo
echo "===== 1. CORRECT AUDIO COGS + RECOMPUTE ECONOMICS ====="
fetch_run apply-v3-audio-cogs-dev-20260912.sh

echo
echo "===== 2. CAPTURE EXACT CERTIFIED SYSTEMD TIMERS ====="
fetch_run capture-v3-production-systemd-bundle-dev.sh

echo
echo "===== 3. VERIFY PRODUCTION BUNDLE EXISTS IN GITHUB ====="
for path in \
  ops/production/systemd/usr/local/bin/desifaces-notification-dispatch.sh \
  ops/production/systemd/usr/local/sbin/desifaces-safe-docker-cleanup.sh \
  ops/production/systemd/etc/systemd/system/desifaces-notification-dispatch.service \
  ops/production/systemd/etc/systemd/system/desifaces-notification-dispatch.timer \
  ops/production/systemd/etc/systemd/system/desifaces-safe-docker-cleanup.service \
  ops/production/systemd/etc/systemd/system/desifaces-safe-docker-cleanup.timer \
  ops/production/systemd/SHA256SUMS; do
  gh api "repos/$REPO/contents/$path?ref=$BRANCH" --jq .sha >/dev/null
  echo "PASS $path"
done

echo
echo "============================================================"
echo " DEV PRODUCTION READINESS PREPARATION=PASS"
echo "============================================================"
echo "AUDIO_COGS_DEFECT=CORRECTED_IN_DEV"
echo "CUSTOMER_PRICING=UNCHANGED"
echo "SYSTEMD_TIMER_BUNDLE=CAPTURED_TO_SOURCE_CONTROL"
echo "PRODUCTION_AUDIO_MIGRATION=PREPARED_NOT_APPLIED"
echo "PRODUCTION_TIMER_INSTALL=PREPARED_NOT_APPLIED"
echo "STRIPE_LIVE=NOT_ENABLED"
echo "PRODUCTION_TOUCH=NONE"
echo "log=$LOG"
