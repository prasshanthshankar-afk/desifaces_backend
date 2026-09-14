#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_SHA="3c6e2f1d35146dc85073288ba016143317b9df13"
FACE_API="df-svc-face"
FACE_WORKER="df-svc-face-worker"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="/tmp/df-face-runtime-hotfix-${STAMP}"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
for C in "$FACE_API" "$FACE_WORKER"; do
  docker inspect "$C" >/dev/null 2>&1 || fail "$C missing"
  [[ "$(docker inspect -f '{{.State.Status}}' "$C")" == "running" ]] || fail "$C not running"
done

mkdir -p "$TMP/src" "$TMP/backup-api/app/api/routes" "$TMP/backup-api/app/api" "$TMP/backup-worker/app/workers"

echo "============================================================"
echo " desifaces — FACE RUNTIME LAUNCH HOTFIX V2"
echo " source_sha=$SOURCE_SHA"
echo " scope=FACE_API+FACE_WORKER_ONLY"
echo " compose_dependency=NONE"
echo "============================================================"

# Safety gate: do not interrupt an active provider-backed Face render.
echo "===== 1. ACTIVE FACE JOB SAFETY GATE ====="
ACTIVE=""
for _ in $(seq 1 12); do
  ACTIVE="$(docker exec -i "$FACE_API" python - <<'PY'
import asyncio, os
import asyncpg
async def main():
    dsn=(os.getenv('DATABASE_URL') or os.getenv('POSTGRES_DSN') or '').strip()
    dsn=dsn.replace('postgresql+asyncpg://','postgresql://',1)
    if not dsn:
        print('ERROR'); return
    conn=await asyncpg.connect(dsn)
    try:
        n=await conn.fetchval("select count(*) from public.studio_jobs where studio_type='face' and status='running'")
        print(int(n or 0))
    finally:
        await conn.close()
asyncio.run(main())
PY
)"
  [[ "$ACTIVE" != "ERROR" ]] || fail "cannot inspect active Face jobs"
  echo "active_face_jobs=$ACTIVE"
  [[ "$ACTIVE" == "0" ]] && break
  sleep 5
done
[[ "$ACTIVE" == "0" ]] || fail "Face jobs still running; production left unchanged"
echo "ACTIVE_FACE_JOB_GATE=PASS"

# Freeze queue consumption after proving nothing is running. New requests can remain safely queued.
docker stop -t 15 "$FACE_WORKER" >/dev/null
WORKER_STOPPED=YES

echo "===== 2. FETCH PINNED SOURCE ====="
fetch(){
  local rel="$1" out="$2"
  curl -fsSL --connect-timeout 5 --max-time 20 \
    "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/${rel}" \
    -o "$out"
}
fetch "services/svc-face/app/app/api/__init__.py" "$TMP/src/api-init.py"
fetch "services/svc-face/app/app/api/routes/canonical_face_assets.py" "$TMP/src/canonical_face_assets.py"
fetch "services/svc-face/app/app/workers/face_worker.py" "$TMP/src/face_worker.py"
python3 -m py_compile "$TMP/src/api-init.py" "$TMP/src/canonical_face_assets.py" "$TMP/src/face_worker.py"
echo "PINNED_SOURCE=PASS"

# Back up current runtime files for exact rollback.
docker cp "$FACE_API:/app/app/api/__init__.py" "$TMP/backup-api/app/api/__init__.py"
if docker exec "$FACE_API" test -f /app/app/api/routes/canonical_face_assets.py; then
  docker cp "$FACE_API:/app/app/api/routes/canonical_face_assets.py" "$TMP/backup-api/app/api/routes/canonical_face_assets.py"
  echo YES > "$TMP/canonical-existed"
else
  echo NO > "$TMP/canonical-existed"
fi
docker cp "$FACE_WORKER:/app/app/workers/face_worker.py" "$TMP/backup-worker/app/workers/face_worker.py"

rollback(){
  code=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker cp "$TMP/backup-api/app/api/__init__.py" "$FACE_API:/app/app/api/__init__.py" >/dev/null 2>&1 || true
  if [[ "$(cat "$TMP/canonical-existed" 2>/dev/null || echo NO)" == "YES" ]]; then
    docker cp "$TMP/backup-api/app/api/routes/canonical_face_assets.py" "$FACE_API:/app/app/api/routes/canonical_face_assets.py" >/dev/null 2>&1 || true
  else
    docker exec "$FACE_API" rm -f /app/app/api/routes/canonical_face_assets.py >/dev/null 2>&1 || true
  fi
  docker cp "$TMP/backup-worker/app/workers/face_worker.py" "$FACE_WORKER:/app/app/workers/face_worker.py" >/dev/null 2>&1 || true
  docker restart "$FACE_API" >/dev/null 2>&1 || true
  docker start "$FACE_WORKER" >/dev/null 2>&1 || true
  echo "ROLLBACK_COMPLETE=YES"
  exit "$code"
}
trap rollback ERR

echo "===== 3. PATCH ONLY FACE RUNTIMES ====="
docker cp "$TMP/src/api-init.py" "$FACE_API:/app/app/api/__init__.py"
docker cp "$TMP/src/canonical_face_assets.py" "$FACE_API:/app/app/api/routes/canonical_face_assets.py"
docker cp "$TMP/src/face_worker.py" "$FACE_WORKER:/app/app/workers/face_worker.py"
echo "FACE_RUNTIME_FILES_PATCHED=PASS"

# Restart API to mount new route; start worker with bounded parallel runtime.
docker restart "$FACE_API" >/dev/null
docker start "$FACE_WORKER" >/dev/null

echo "===== 4. HEALTH ====="
API_STATE=""; API_HEALTH=""; WORKER_STATE=""; WORKER_HEALTH=""
for _ in $(seq 1 40); do
  API_STATE="$(docker inspect -f '{{.State.Status}}' "$FACE_API" 2>/dev/null || true)"
  API_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$FACE_API" 2>/dev/null || true)"
  WORKER_STATE="$(docker inspect -f '{{.State.Status}}' "$FACE_WORKER" 2>/dev/null || true)"
  WORKER_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$FACE_WORKER" 2>/dev/null || true)"
  if [[ "$API_STATE" == "running" && ( "$API_HEALTH" == "healthy" || "$API_HEALTH" == "no-healthcheck" ) \
     && "$WORKER_STATE" == "running" && ( "$WORKER_HEALTH" == "healthy" || "$WORKER_HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 5
done
[[ "$API_STATE" == "running" ]] || fail "Face API not running"
[[ "$API_HEALTH" == "healthy" || "$API_HEALTH" == "no-healthcheck" ]] || fail "Face API unhealthy: $API_HEALTH"
[[ "$WORKER_STATE" == "running" ]] || fail "Face worker not running"
[[ "$WORKER_HEALTH" == "healthy" || "$WORKER_HEALTH" == "no-healthcheck" ]] || fail "Face worker unhealthy: $WORKER_HEALTH"
echo "FACE_API_HEALTH=PASS"
echo "FACE_WORKER_HEALTH=PASS"

echo "===== 5. LIVE CONTRACT CERTIFICATION ====="
docker exec -i "$FACE_API" python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
assert '/api/face/assets/{media_id}/read-url' in paths, sorted(p for p in paths if '/api/face/' in p)
print('FACE_CANONICAL_READ_URL_ROUTE=PASS')
PY

docker exec -i "$FACE_WORKER" python - <<'PY'
import inspect
from app.workers.face_worker import WorkerProcess, _worker_concurrency
src=inspect.getsource(WorkerProcess)
assert _worker_concurrency() == 2, _worker_concurrency()
assert 'limit=capacity' in src
assert 'asyncio.create_task' in src
assert 'limit=1' not in src
print('FACE_JOB_CONCURRENCY=2')
print('FACE_JOB_PARALLELISM=PASS')
PY

# Resolve one real existing Face asset through the same canonical function Director/Fusion needs.
docker exec -i "$FACE_API" python - <<'PY'
import asyncio
from app.db import get_pool
from app.api.routes.canonical_face_assets import get_face_asset_read_url
async def main():
    pool=await get_pool()
    async with pool.acquire() as conn:
        row=await conn.fetchrow("""
          select id::text as id
          from public.media_assets
          where kind='face_image'
          order by created_at desc
          limit 1
        """)
    if not row:
        print('FACE_EXISTING_ASSET_READ_URL=SKIPPED_NO_ASSET')
        return
    result=await get_face_asset_read_url(row['id'], 'svc-fusion-extension')
    assert str(result.read_url).startswith('https://')
    assert result.media_id == row['id']
    print('FACE_EXISTING_ASSET_READ_URL=PASS')
asyncio.run(main())
PY

trap - ERR

echo "============================================================"
echo "MULTI_PERSON_FACE_READ_URL_FIX=DEPLOYED"
echo "FACE_JOB_PARALLEL_EXECUTION=DEPLOYED"
echo "FACE_API_STATE=$API_STATE"
echo "FACE_API_HEALTH=$API_HEALTH"
echo "FACE_WORKER_STATE=$WORKER_STATE"
echo "FACE_WORKER_HEALTH=$WORKER_HEALTH"
echo "COMPOSE_TOUCH=NONE"
echo "DB_SCHEMA_TOUCH=NONE"
echo "PRICING_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "FACE_LAUNCH_HOTFIX=PASS"
echo "============================================================"
