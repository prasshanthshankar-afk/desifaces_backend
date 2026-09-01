#!/usr/bin/env bash
set -euo pipefail

ROOT="${V3_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BRANCH="${FUSION_FIX_BRANCH:-fix/v3-fusion-reusable-input-resolution-20260901}"
API="${FUSION_API_CONTAINER:-df-v3-svc-fusion}"
WORKER="${FUSION_WORKER_CONTAINER:-df-v3-svc-fusion-worker}"
PORT="${FUSION_HEALTH_PORT:-18002}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="/tmp/v3-fusion-input-resolution-${STAMP}"
mkdir -p "$TMP"

rollback() {
  set +e
  if [ -f "$TMP/api-artifacts_repo.py" ]; then docker cp "$TMP/api-artifacts_repo.py" "$API":/app/app/repos/artifacts_repo.py >/dev/null 2>&1; fi
  if [ -f "$TMP/worker-artifacts_repo.py" ]; then docker cp "$TMP/worker-artifacts_repo.py" "$WORKER":/app/app/repos/artifacts_repo.py >/dev/null 2>&1; fi
  docker restart "$API" "$WORKER" >/dev/null 2>&1 || true
  echo "ROLLBACK: restored prior Fusion resolver files"
}
trap 'rc=$?; if [ $rc -ne 0 ]; then rollback; fi; exit $rc' EXIT

echo "============================================================"
echo " desifaces V3 FUSION REUSABLE INPUT RESOLUTION HOTFIX"
echo "============================================================"

echo
echo "===== 1. SOURCE ====="
cd "$ROOT"
git fetch -q origin "$BRANCH"
git show "origin/$BRANCH:services/svc-fusion/app/app/repos/artifacts_repo.py" > "$TMP/artifacts_repo.py"
python3 -m py_compile "$TMP/artifacts_repo.py"
grep -q "FROM public.media_assets" "$TMP/artifacts_repo.py"
echo "PASS: resolver source supports artifacts + media_assets"

echo
echo "===== 2. PRESERVE LIVE FILES ====="
docker cp "$API":/app/app/repos/artifacts_repo.py "$TMP/api-artifacts_repo.py"
docker cp "$WORKER":/app/app/repos/artifacts_repo.py "$TMP/worker-artifacts_repo.py"
sha256sum "$TMP/api-artifacts_repo.py" "$TMP/worker-artifacts_repo.py" > "$TMP/rollback.sha256"
echo "PASS: rollback copies captured at $TMP"

echo
echo "===== 3. PATCH FUSION API + WORKER ONLY ====="
docker cp "$TMP/artifacts_repo.py" "$API":/app/app/repos/artifacts_repo.py
docker cp "$TMP/artifacts_repo.py" "$WORKER":/app/app/repos/artifacts_repo.py
docker restart "$API" "$WORKER" >/dev/null
echo "PASS: Fusion API + worker restarted"

echo
echo "===== 4. HEALTH ====="
ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" -eq 1 ]
echo "PASS: Fusion API HTTP_200"

echo
echo "===== 5. LOADED CONTRACT ====="
for c in "$API" "$WORKER"; do
  docker exec "$c" python -c 'import inspect; from app.repos.artifacts_repo import ArtifactsRepo; s=inspect.getsource(ArtifactsRepo.get_artifact_by_id); assert "public.media_assets" in s and "public.artifacts" in s; print("PASS: reusable input resolver loaded")'
done

echo
echo "===== 6. PRESERVE MULTI-PERSON PRICING HOTFIX ====="
docker exec "$API" python -c 'import inspect, app.main; from app.services.fusion_orchestrator import FusionOrchestrator; sig=inspect.signature(FusionOrchestrator._build_initial_pricing_block); print("pricing_signature=", sig); assert len(sig.parameters)==2; print("PASS: pricing wrapper remains (self, req)")'

echo
echo "============================================================"
echo " FUSION REUSABLE INPUT RESOLUTION: HOTFIX READY"
echo "============================================================"
echo "branch=$BRANCH"
echo "rollback_dir=$TMP"
echo "api=healthy"
echo "worker=patched"
echo "db=untouched"
echo "redis=untouched"
echo "web=untouched"
echo "============================================================"

trap - EXIT
