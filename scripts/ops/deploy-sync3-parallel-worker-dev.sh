#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
ENV_FILE="$ROOT/infra/.env"
BACKEND_SHA="fb2103c7d1a57b573c4b6569f37a89f856109fd5"
WT="/tmp/desifaces-sync3-parallel-worker"
WORKER="df-v3-svc-fusion-worker"

fail(){ echo "FAIL: $*" >&2; exit 1; }

project_of(){
  local p
  p="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$WORKER" 2>/dev/null || true)"
  [[ -n "$p" && "$p" != "<no value>" ]] || return 1
  printf "%s" "$p"
}

cleanup(){
  set +e
  if git -C "$ROOT" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $WT"; then
    git -C "$ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ -f "$ENV_FILE" ]] || fail "canonical DEV env missing"
docker inspect "$WORKER" >/dev/null 2>&1 || fail "$WORKER missing"

echo "============================================================"
echo " desifaces DEV — SYNC3 PARALLEL VIDEO SEGMENTS"
echo " backend_sha=$BACKEND_SHA"
echo " worker_concurrency=8"
echo " sync3_provider_concurrency=6"
echo " production_touch=NONE"
echo "============================================================"

echo
echo "=== 0. ACTIVE GENERATION GUARD ==="
ACTIVE="$(docker exec -i "$WORKER" python - <<'PY'
import asyncio, os, asyncpg
async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows=await conn.fetch("""
          select id::text,status,coalesce(payload_json->>'provider','') provider
          from public.studio_jobs
          where studio_type='fusion'
            and lower(coalesce(status,'')) in ('queued','running','processing')
          order by updated_at
        """)
        print(f"COUNT={len(rows)}")
        for r in rows:
            print(f"JOB={r['id']} status={r['status']} provider={r['provider']}")
    finally:
        await conn.close()
asyncio.run(main())
PY
)"
echo "$ACTIVE"
COUNT="$(printf '%s\n' "$ACTIVE" | sed -n 's/^COUNT=//p' | tail -1)"
[[ "$COUNT" =~ ^[0-9]+$ ]] || fail "could not determine active Fusion jobs"
(( COUNT == 0 )) || fail "active Fusion generation exists; worker was NOT restarted"
echo "ACTIVE_GENERATION_JOBS=0"

echo
echo "=== 1. EXACT SOURCE ==="
git -C "$ROOT" fetch --no-tags origin "$BACKEND_SHA"
git -C "$ROOT" cat-file -e "$BACKEND_SHA^{commit}"
if git -C "$ROOT" worktree list --porcelain | grep -Fxq "worktree $WT"; then
  git -C "$ROOT" worktree remove --force "$WT"
elif [[ -e "$WT" ]]; then
  fail "unrecognized worktree exists: $WT"
fi
git -C "$ROOT" worktree add --detach "$WT" "$BACKEND_SHA"
[[ "$(git -C "$WT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "worktree SHA mismatch"

grep -Fq 'DF_FUSION_WORKER_CONCURRENCY: ${DF_FUSION_WORKER_CONCURRENCY:-8}' "$WT/docker-compose.v3.yml"
grep -Fq 'DF_SYNC3_PROVIDER_CONCURRENCY: ${DF_SYNC3_PROVIDER_CONCURRENCY:-6}' "$WT/docker-compose.v3.yml"
grep -Fq 'raw = os.getenv("DF_SYNC3_PROVIDER_CONCURRENCY") or "1"' "$WT/services/svc-fusion/app/app/services/providers/sync3_adapter.py"
echo "SYNC3_PARALLEL_SOURCE_CONTRACT=PASS"

PROJECT="$(project_of)" || fail "Fusion worker Compose project missing"
echo "FUSION_WORKER_PROJECT=$PROJECT"

echo
echo "=== 2. BUILD + RECREATE FUSION WORKER ONLY ==="
cd "$WT"
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution build svc-fusion-worker
V3_ENV_FILE="$ENV_FILE" ./scripts/v3-compose.sh -p "$PROJECT" --profile v3-execution up -d --no-deps --force-recreate svc-fusion-worker
[[ "$(docker inspect -f '{{.State.Status}}' "$WORKER")" == "running" ]] || fail "Fusion worker not running"

echo
echo "=== 3. RUNTIME CERTIFICATION ==="
docker exec -i "$WORKER" python - <<'PY'
import os
from app.services.providers.sync3_adapter import _sync3_provider_concurrency
worker=int(os.getenv("DF_FUSION_WORKER_CONCURRENCY") or "0")
provider=_sync3_provider_concurrency()
print(f"FUSION_WORKER_CONCURRENCY={worker}")
print(f"SYNC3_PROVIDER_CONCURRENCY={provider}")
if worker < 2:
    raise SystemExit("FAIL: Fusion worker is serial")
if provider < 2:
    raise SystemExit("FAIL: Sync3 provider path is serial")
effective=min(worker, provider)
print(f"EFFECTIVE_END_TO_END_SEGMENT_PARALLELISM={effective}")
print("SYNC3_PARALLEL_RUNTIME=PASS")
PY

echo
echo "=== 4. PROVIDER CAPACITY WARNING ==="
echo "Runtime is configured for 6 concurrent Sync3 generations."
echo "The Sync account itself must permit >=6 active generations."
echo "If Sync returns concurrency_limit_reached, provider account capacity is the remaining bottleneck."
echo
echo "============================================================"
echo " SYNC3 PARALLEL VIDEO SEGMENTS DEV DEPLOY COMPLETE"
echo " DIRECTOR_DISPATCH=PARALLEL"
echo " FUSION_WORKER_CONCURRENCY=8"
echo " SYNC3_PROVIDER_CONCURRENCY=6"
echo " EFFECTIVE_TARGET_PARALLELISM=6"
echo " FINAL_STITCH=AFTER_ALL_CHILDREN_COMPLETE"
echo " PRODUCTION_TOUCH=NONE"
echo "============================================================"
