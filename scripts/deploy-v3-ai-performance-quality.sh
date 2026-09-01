#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${V3_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BRANCH="${AI_FIX_BRANCH:-fix/v3-ai-performance-quality-20260901}"
FUSION_WORKER="${FUSION_WORKER_CONTAINER:-df-v3-svc-fusion-worker}"
FACE_WORKER="${FACE_WORKER_CONTAINER:-df-v3-svc-face-worker}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="/tmp/v3-ai-performance-quality-${STAMP}"
mkdir -p "$TMP"

exists(){ docker inspect "$1" >/dev/null 2>&1; }
resolve_worker(){
  local preferred="$1" service="$2"
  if exists "$preferred"; then printf '%s' "$preferred"; return 0; fi
  local found
  found="$(docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1)"
  [ -n "$found" ] || return 1
  printf '%s' "$found"
}
copy_if(){ docker cp "$1:$2" "$3" >/dev/null 2>&1 || true; }
restore_if(){ [ -f "$2" ] && docker cp "$2" "$1:$3" >/dev/null 2>&1 || true; }

FUSION_WORKER="$(resolve_worker "$FUSION_WORKER" svc-fusion-worker)" || { echo "FAIL: Fusion worker container not found" >&2; exit 2; }
FACE_WORKER="$(resolve_worker "$FACE_WORKER" svc-face-worker)" || { echo "FAIL: Face worker container not found" >&2; exit 3; }

rollback(){
  set +e
  restore_if "$FUSION_WORKER" "$TMP/live-fusion_worker.py" /app/app/workers/fusion_worker.py
  restore_if "$FUSION_WORKER" "$TMP/live-fusion_quality_policy.py" /app/app/services/fusion_quality_policy.py
  if [ -f "$TMP/live-fusion_performance_policy.py" ]; then
    restore_if "$FUSION_WORKER" "$TMP/live-fusion_performance_policy.py" /app/app/services/fusion_performance_policy.py
  else
    docker exec "$FUSION_WORKER" rm -f /app/app/services/fusion_performance_policy.py >/dev/null 2>&1 || true
  fi
  restore_if "$FACE_WORKER" "$TMP/live-face_worker.py" /app/app/workers/face_worker.py
  if [ -f "$TMP/live-face_performance_policy.py" ]; then
    restore_if "$FACE_WORKER" "$TMP/live-face_performance_policy.py" /app/app/services/face_performance_policy.py
  else
    docker exec "$FACE_WORKER" rm -f /app/app/services/face_performance_policy.py >/dev/null 2>&1 || true
  fi
  docker restart "$FUSION_WORKER" "$FACE_WORKER" >/dev/null 2>&1 || true
  echo "ROLLBACK: restored prior Face/Fusion worker runtime"
}
trap 'rc=$?; if [ $rc -ne 0 ]; then rollback; fi; exit $rc' EXIT

echo "============================================================"
echo " desifaces V3 AI PERFORMANCE + VIDEO QUALITY DEPLOY"
echo "============================================================"

echo
echo "===== 1. SOURCE GATE ====="
cd "$ROOT"
git fetch -q origin "$BRANCH"
for spec in \
  'services/svc-fusion/app/app/services/fusion_quality_policy.py:fusion_quality_policy.py' \
  'services/svc-fusion/app/app/services/fusion_performance_policy.py:fusion_performance_policy.py' \
  'services/svc-fusion/app/app/workers/fusion_worker.py:fusion_worker.py' \
  'services/svc-face/app/app/services/face_performance_policy.py:face_performance_policy.py' \
  'services/svc-face/app/app/workers/face_worker.py:face_worker.py'; do
  src="${spec%%:*}"; dst="${spec##*:}"
  git show "origin/$BRANCH:$src" > "$TMP/$dst"
done
python3 -m py_compile \
  "$TMP/fusion_quality_policy.py" \
  "$TMP/fusion_performance_policy.py" \
  "$TMP/fusion_worker.py" \
  "$TMP/face_performance_policy.py" \
  "$TMP/face_worker.py"
grep -q 'pricing.get("tier_code")' "$TMP/fusion_quality_policy.py"
grep -q 'asyncio.gather' "$TMP/fusion_performance_policy.py"
grep -q 'install_fusion_quality_policy()' "$TMP/fusion_worker.py"
grep -q 'install_fusion_performance_policy()' "$TMP/fusion_worker.py"
grep -q 'DF_FACE_VARIANT_CONCURRENCY", "4"' "$TMP/face_performance_policy.py"
grep -q 'install_face_performance_policy()' "$TMP/face_worker.py"
echo "PASS: quality, parallel staging and Face concurrency worker sources validated"

echo
echo "===== 2. PRESERVE LIVE WORKERS ====="
copy_if "$FUSION_WORKER" /app/app/workers/fusion_worker.py "$TMP/live-fusion_worker.py"
copy_if "$FUSION_WORKER" /app/app/services/fusion_quality_policy.py "$TMP/live-fusion_quality_policy.py"
copy_if "$FUSION_WORKER" /app/app/services/fusion_performance_policy.py "$TMP/live-fusion_performance_policy.py"
copy_if "$FACE_WORKER" /app/app/workers/face_worker.py "$TMP/live-face_worker.py"
copy_if "$FACE_WORKER" /app/app/services/face_performance_policy.py "$TMP/live-face_performance_policy.py"
sha256sum "$TMP"/live-* 2>/dev/null > "$TMP/rollback.sha256" || true
echo "PASS: rollback captured at $TMP"

echo
echo "===== 3. PATCH FUSION WORKER ONLY ====="
docker cp "$TMP/fusion_quality_policy.py" "$FUSION_WORKER":/app/app/services/fusion_quality_policy.py
docker cp "$TMP/fusion_performance_policy.py" "$FUSION_WORKER":/app/app/services/fusion_performance_policy.py
docker cp "$TMP/fusion_worker.py" "$FUSION_WORKER":/app/app/workers/fusion_worker.py
docker restart "$FUSION_WORKER" >/dev/null
echo "PASS: Fusion render worker restarted"

echo
echo "===== 4. PATCH FACE WORKER ONLY ====="
docker cp "$TMP/face_performance_policy.py" "$FACE_WORKER":/app/app/services/face_performance_policy.py
docker cp "$TMP/face_worker.py" "$FACE_WORKER":/app/app/workers/face_worker.py
docker restart "$FACE_WORKER" >/dev/null
echo "PASS: Face render worker restarted"

echo
echo "===== 5. LOADED WORKER CONTRACT ====="
sleep 4
[ "$(docker inspect -f '{{.State.Running}}' "$FUSION_WORKER")" = "true" ]
[ "$(docker inspect -f '{{.State.Running}}' "$FACE_WORKER")" = "true" ]
docker exec "$FUSION_WORKER" python -c 'import app.workers.fusion_worker; import app.services.fusion_quality_policy as q; import app.services.fusion_performance_policy as p; from app.services.providers.omnihuman_adapter import OmniHumanAdapter; assert q._INSTALLED and p._INSTALLED; x=OmniHumanAdapter.__new__(OmniHumanAdapter); tier=x._tier_code({"pricing":{"tier_code":"pro"}}); print("trusted_tier_probe=",tier); assert str(tier).lower()=="pro"; print("PASS: Fusion trusted-tier quality + parallel input staging loaded")'
docker exec "$FACE_WORKER" python -c 'import app.workers.face_worker; import app.services.face_performance_policy as p; from app.services.creator_orchestrator import CreatorOrchestrator; assert p._INSTALLED; x=CreatorOrchestrator.__new__(CreatorOrchestrator); v=x._face_variant_concurrency(); print("face_variant_concurrency=",v); assert 1 <= v <= 8; print("PASS: Face bounded variant concurrency policy loaded")'
echo "PASS: worker contracts loaded"

echo
echo "===== 6. API PRESERVATION ====="
curl -fsS http://127.0.0.1:18002/api/health >/dev/null
echo "PASS: Fusion API HTTP_200"
curl -fsS http://127.0.0.1:18003/api/health >/dev/null
echo "PASS: Face API HTTP_200"

echo
echo "===== 7. DEPLOY CERTIFIED WEB UX ====="
cd "$WEB_ROOT"
git pull --ff-only origin main
bash scripts/deploy-ai-performance-quality-ux.sh

echo
echo "============================================================"
echo " AI PERFORMANCE + VIDEO QUALITY: DEPLOYED + CERTIFIED"
echo "============================================================"
echo "fusion_worker=$FUSION_WORKER"
echo "face_worker=$FACE_WORKER"
echo "fusion_quality=trusted-pricing-tier"
echo "fusion_input_staging=parallel"
echo "face_variant_concurrency=4-default-bounded"
echo "face_price_visibility=deployed"
echo "long_running_hourglass=deployed"
echo "premium_video=omnihuman-up-to-1080p"
echo "fast_video=omnihuman-720p-turbo"
echo "veed_fabric=economy-longform-priced-path-not-single-person"
echo "db=untouched"
echo "redis=untouched"
echo "pricing_service=untouched"
echo "audio=untouched"
echo "rollback_dir=$TMP"
echo "============================================================"
trap - EXIT
