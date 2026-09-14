#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_SHA="3c6e2f1d35146dc85073288ba016143317b9df13"
FACE_API="df-svc-face"
FACE_WORKER="df-svc-face-worker"
ENV_FILE="/home/azureuser/workspace/desifaces/infra/.env"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
for C in "$FACE_API" "$FACE_WORKER"; do
  docker inspect "$C" >/dev/null 2>&1 || fail "$C missing"
  [[ "$(docker inspect -f '{{.State.Status}}' "$C")" == "running" ]] || fail "$C not running"
done
[[ -f "$ENV_FILE" ]] || fail "production env file missing: $ENV_FILE"

API_SERVICE="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$FACE_API")"
WORKER_SERVICE="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$FACE_WORKER")"
WORKDIR="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$FACE_API")"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$FACE_API")"
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$FACE_API")"

[[ -n "$API_SERVICE" && "$API_SERVICE" != "<no value>" ]] || fail "Face API compose service missing"
[[ -n "$WORKER_SERVICE" && "$WORKER_SERVICE" != "<no value>" ]] || fail "Face worker compose service missing"
[[ -d "$WORKDIR" ]] || fail "compose workdir missing: $WORKDIR"
[[ -n "$CONFIG_FILES" && "$CONFIG_FILES" != "<no value>" ]] || fail "compose config files missing"

IFS=',' read -r -a FILES <<< "$CONFIG_FILES"
COMPOSE=(docker compose --project-name "$PROJECT" --env-file "$ENV_FILE")
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || fail "compose file missing: $f"
  COMPOSE+=(-f "$f")
done

cd "$WORKDIR"
"${COMPOSE[@]}" config --services | grep -Fxq "$API_SERVICE" || fail "Face API service absent from compose"
"${COMPOSE[@]}" config --services | grep -Fxq "$WORKER_SERVICE" || fail "Face worker service absent from compose"

echo "============================================================"
echo " desifaces — FACE PARALLELISM + MULTI-PERSON READ-URL HOTFIX"
echo " source_sha=$SOURCE_SHA"
echo " scope=FACE_API+FACE_WORKER_ONLY"
echo " db_schema_change=NONE"
echo " pricing_change=NONE"
echo "============================================================"

# Never interrupt an actively-rendering Face job. Queued jobs are durable and safe.
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
        print('ERROR')
        return
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

# Preserve old images and exact source files for rollback.
OLD_API_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$FACE_API")"
OLD_WORKER_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$FACE_WORKER")"
API_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$FACE_API")"
WORKER_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$FACE_WORKER")"

docker tag "$OLD_API_IMAGE_ID" "desifaces-face-api-rollback:${STAMP}"
docker tag "$OLD_WORKER_IMAGE_ID" "desifaces-face-worker-rollback:${STAMP}"

TARGETS=(
  "services/svc-face/app/app/api/__init__.py"
  "services/svc-face/app/app/api/routes/canonical_face_assets.py"
  "services/svc-face/app/app/workers/face_worker.py"
  "services/svc-face/app/app/tests/test_launch_face_parallel_read_url.py"
)
BACKUP="/tmp/desifaces-face-hotfix-${STAMP}"
mkdir -p "$BACKUP"
for p in "${TARGETS[@]}"; do
  mkdir -p "$BACKUP/$(dirname "$p")" "$(dirname "$WORKDIR/$p")"
  [[ -f "$WORKDIR/$p" ]] && cp -a "$WORKDIR/$p" "$BACKUP/$p" || true
  curl -fsSL --connect-timeout 5 --max-time 20 \
    "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/${p}" \
    -o "$WORKDIR/$p"
done

rollback(){
  code=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  for p in "${TARGETS[@]}"; do
    if [[ -f "$BACKUP/$p" ]]; then
      cp -a "$BACKUP/$p" "$WORKDIR/$p"
    else
      rm -f "$WORKDIR/$p"
    fi
  done
  docker tag "$OLD_API_IMAGE_ID" "$API_IMAGE_REF" || true
  docker tag "$OLD_WORKER_IMAGE_ID" "$WORKER_IMAGE_REF" || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$API_SERVICE" "$WORKER_SERVICE" >/dev/null 2>&1 || true
  echo "ROLLBACK_COMPLETE=YES"
  exit "$code"
}
trap rollback ERR

echo "===== 2. SOURCE CONTRACT ====="
python3 -m py_compile \
  services/svc-face/app/app/api/routes/canonical_face_assets.py \
  services/svc-face/app/app/api/__init__.py \
  services/svc-face/app/app/workers/face_worker.py
python3 - <<'PY'
from pathlib import Path
api=Path('services/svc-face/app/app/api/__init__.py').read_text()
route=Path('services/svc-face/app/app/api/routes/canonical_face_assets.py').read_text()
worker=Path('services/svc-face/app/app/workers/face_worker.py').read_text()
assert '@router.get("/assets/{media_id}/read-url"' in route
assert 'get_readonly_sas_url' in route
assert 'canonical_face_assets_router' in api
assert 'DF_FACE_JOB_CONCURRENCY' in worker
assert 'limit=capacity' in worker
assert 'asyncio.create_task' in worker
assert 'limit=1' not in worker
print('SOURCE_CONTRACT=PASS')
PY

echo "===== 3. BUILD FACE SERVICES ====="
"${COMPOSE[@]}" build "$API_SERVICE" "$WORKER_SERVICE"
echo "FACE_IMAGES_BUILD=PASS"

echo "===== 4. GUARDED RECREATE ====="
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$API_SERVICE" "$WORKER_SERVICE" >/dev/null

API_STATE=""; API_HEALTH=""; WORKER_STATE=""; WORKER_HEALTH=""
for _ in $(seq 1 36); do
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

echo "===== 5. LIVE ROUTE + PARALLELISM CERTIFICATION ====="
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

# Prove an existing generated Face can be resolved to a fresh URL using the new contract.
docker exec -i "$FACE_API" python - <<'PY'
import asyncio
from app.db import get_pool
from app.api.routes.canonical_face_assets import get_face_asset_read_url

async def main():
    pool=await get_pool()
    async with pool.acquire() as conn:
        row=await conn.fetchrow("""
          select id::text as id, user_id::text as user_id
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

echo "face_api_state=$API_STATE"
echo "face_api_health=$API_HEALTH"
echo "face_worker_state=$WORKER_STATE"
echo "face_worker_health=$WORKER_HEALTH"
echo "MULTI_PERSON_FACE_READ_URL_FIX=DEPLOYED"
echo "FACE_JOB_PARALLEL_EXECUTION=DEPLOYED"
echo "DB_SCHEMA_TOUCH=NONE"
echo "PRICING_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "FACE_LAUNCH_HOTFIX=PASS"
