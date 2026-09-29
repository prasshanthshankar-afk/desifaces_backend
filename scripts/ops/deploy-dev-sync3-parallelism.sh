#!/usr/bin/env bash
set -Eeuo pipefail

RUNTIME_ROOT="${RUNTIME_ROOT:-$HOME/workspace/desifaces-runtime}"
ENV_FILE="${ENV_FILE:-$RUNTIME_ROOT/infra/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-$RUNTIME_ROOT/docker-compose.yml}"
SYNC3_PROVIDER_CAPACITY="${SYNC3_PROVIDER_CAPACITY:-3}"
DRAIN_WAIT_SECONDS="${DRAIN_WAIT_SECONDS:-1800}"

fail(){ printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
log(){ printf '%s\n' "$*"; }

[[ "$(hostname -s)" == "desifaces-dev" ]] || fail "DEV host desifaces-dev required"
[[ -f "$ENV_FILE" ]] || fail "runtime env missing: $ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || fail "runtime compose missing: $COMPOSE_FILE"
docker inspect df-svc-fusion >/dev/null 2>&1 || fail "df-svc-fusion missing"
docker inspect df-svc-fusion-worker >/dev/null 2>&1 || fail "df-svc-fusion-worker missing"

[[ "$SYNC3_PROVIDER_CAPACITY" =~ ^[0-9]+$ ]] || fail "SYNC3_PROVIDER_CAPACITY must be an integer"
(( SYNC3_PROVIDER_CAPACITY >= 1 && SYNC3_PROVIDER_CAPACITY <= 15 )) || fail "SYNC3_PROVIDER_CAPACITY must be 1..15"

log "============================================================"
log " desifaces DEV — SYNC3 PROVIDER PARALLELISM"
log "============================================================"
log "provider_capacity=$SYNC3_PROVIDER_CAPACITY"
log "core_fusion_worker_concurrency=8"
log "production=UNTOUCHED"

active_fusion(){
  docker exec -i df-svc-fusion python - <<'PY'
import asyncio
from app.db import get_pool
async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "select count(*) from public.studio_jobs "
            "where studio_type='fusion' and status in ('running','processing')"
        )
    print(int(n or 0))
asyncio.run(main())
PY
}

log ""
log "===== 1. CURRENT CONTRACT ====="
docker exec df-svc-fusion-worker bash -lc '
env | grep -E "^(DF_FUSION_WORKER_CONCURRENCY|DF_SYNC3_PROVIDER_CONCURRENCY|DF_SYNC3_CONCURRENCY_WAIT_SECONDS|DF_SYNC3_MODEL_ID)=" | sort
'

log ""
log "===== 2. DRAIN CURRENT FUSION WORK ====="
deadline=$(( $(date +%s) + DRAIN_WAIT_SECONDS ))
while :; do
  ACTIVE="$(active_fusion)"
  log "active_core_fusion_jobs=$ACTIVE"
  [[ "$ACTIVE" == "0" ]] && break
  (( $(date +%s) < deadline )) || fail "Fusion work did not drain; no runtime mutation performed"
  sleep 10
done

log ""
log "===== 3. UPDATE DEV ENV ====="
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/tmp/desifaces-dev-env-before-sync3-parallelism-$STAMP"
cp "$ENV_FILE" "$BACKUP"

python3 - "$ENV_FILE" "$SYNC3_PROVIDER_CAPACITY" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
capacity = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()

updates = {
    "DF_SYNC3_PROVIDER_CONCURRENCY": capacity,
    "DF_FUSION_WORKER_CONCURRENCY": "8",
}

seen = set()
out = []
for line in lines:
    stripped = line.strip()
    replaced = False
    for key, value in updates.items():
        if stripped.startswith(key + "="):
            out.append(f"{key}={value}")
            seen.add(key)
            replaced = True
            break
    if not replaced:
        out.append(line)

for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")

path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

log "env_backup=$BACKUP"
log "DEV_ENV_UPDATE=PASS"

log ""
log "===== 4. RECREATE FUSION WORKER ONLY ====="
cd "$RUNTIME_ROOT"
docker compose   --env-file "$ENV_FILE"   -f "$COMPOSE_FILE"   up -d --no-deps --force-recreate svc-fusion-worker

for _ in $(seq 1 30); do
  state="$(docker inspect -f '{{.State.Status}}' df-svc-fusion-worker 2>/dev/null || true)"
  [[ "$state" == "running" ]] && break
  sleep 2
done
[[ "$(docker inspect -f '{{.State.Status}}' df-svc-fusion-worker)" == "running" ]] || fail "Fusion worker failed to start"

log ""
log "===== 5. RUNTIME CERTIFICATION ====="
docker exec -i df-svc-fusion-worker python - <<PY
import inspect
from app.workers import fusion_worker
from app.services.providers.sync3_adapter import Sync3Adapter, _provider_concurrency_limit

worker = fusion_worker._worker_concurrency()
provider = _provider_concurrency_limit()
adapter = Sync3Adapter()
source = inspect.getsource(fusion_worker)

assert worker == 8, worker
assert provider == $SYNC3_PROVIDER_CAPACITY, provider
assert adapter.provider_concurrency == $SYNC3_PROVIDER_CAPACITY, adapter.provider_concurrency
assert "limit=capacity" in source
assert "asyncio.create_task" in source
assert "FIRST_COMPLETED" in source

print(f"CORE_FUSION_PARALLELISM=PASS concurrency={worker}")
print(f"SYNC3_PROVIDER_PARALLELISM=PASS capacity={provider}")
PY

log ""
log "============================================================"
log " DEV_SYNC3_PARALLELISM=PASS"
log " provider_capacity=$SYNC3_PROVIDER_CAPACITY"
log " expected_6_clip_waves=$(( (6 + SYNC3_PROVIDER_CAPACITY - 1) / SYNC3_PROVIDER_CAPACITY ))"
log " production=UNTOUCHED"
log "============================================================"
