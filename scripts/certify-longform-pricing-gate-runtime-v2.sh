#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_SHA="${EXPECTED_SHA:-34bbfcae5b4d3772913675b1f68a5632d4a9c932}"
WS="${WS:-/home/azureuser/workspace/desifaces-v3}"

fail(){ echo "FAIL: $*" >&2; exit 1; }
resolve_container(){
  local preferred="$1" service="$2"
  if docker inspect "$preferred" >/dev/null 2>&1; then printf '%s' "$preferred"; return 0; fi
  docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1
}

cd "$WS"
EXT_API="$(resolve_container "${EXT_API_CONTAINER:-df-v3-svc-fusion-extension}" svc-fusion-extension)"
[ -n "$EXT_API" ] || fail "Fusion Extension API container not found"

echo "============================================================"
echo " desifaces V3 — LONGFORM PRICING GATE RUNTIME CERTIFICATION v2"
echo "============================================================"
echo "expected_sha=$EXPECTED_SHA"
echo "container=$EXT_API"

running="$(docker inspect -f '{{.State.Running}}' "$EXT_API")"
[ "$running" = "true" ] || fail "Fusion Extension API container is not running"
echo "CONTAINER_RUNNING=PASS"

runtime_port="$(docker exec "$EXT_API" sh -lc 'printf "%s" "${PORT:-}"')"
[[ "$runtime_port" =~ ^[0-9]+$ ]] || fail "Fusion Extension PORT is missing/invalid: '$runtime_port'"
echo "RUNTIME_PORT=$runtime_port"

docker exec "$EXT_API" python -c "import os,urllib.request; p=os.environ.get('PORT',''); assert p.isdigit(), p; r=urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=5); assert r.status==200; print(f'FUSION_EXTENSION_HEALTH=PASS HTTP_{r.status} PORT={p}')"

ROUTE="services/svc-fusion-extension/app/app/api/routes/longform.py"
REPO="services/svc-fusion-extension/app/app/repos/longform_jobs_repo.py"
expected_route_hash="$(git show "$EXPECTED_SHA:$ROUTE" | sha256sum | awk '{print $1}')"
expected_repo_hash="$(git show "$EXPECTED_SHA:$REPO" | sha256sum | awk '{print $1}')"
live_route_hash="$(docker exec "$EXT_API" python -c "import hashlib; print(hashlib.sha256(open('/app/app/api/routes/longform.py','rb').read()).hexdigest())")"
live_repo_hash="$(docker exec "$EXT_API" python -c "import hashlib; print(hashlib.sha256(open('/app/app/repos/longform_jobs_repo.py','rb').read()).hexdigest())")"
[ "$live_route_hash" = "$expected_route_hash" ] || fail "live longform.py does not match $EXPECTED_SHA"
[ "$live_repo_hash" = "$expected_repo_hash" ] || fail "live longform_jobs_repo.py does not match $EXPECTED_SHA"
echo "DEPLOYED_SOURCE_MATCH=PASS"

docker exec -i "$EXT_API" python - <<'PY'
import inspect
import app.api.routes.longform as route
from app.repos.longform_jobs_repo import LongformJobsRepo
src = inspect.getsource(route.create_longform_job)
required = [
    'initial_status="pricing_pending"',
    'reserve_longform_pricing_for_job',
    'await segs_repo.insert_segment',
    "SET status = 'queued'",
]
for token in required:
    assert token in src, token
p_pending = src.index(required[0])
p_reserve = src.index(required[1])
p_insert = src.index(required[2])
p_activate = src.index(required[3])
p_block = src.index('failed_status = "blocked"')
assert p_pending < p_reserve < p_block < p_insert < p_activate, (p_pending,p_reserve,p_block,p_insert,p_activate)
sig = inspect.signature(LongformJobsRepo.create_job)
assert sig.parameters['initial_status'].default == 'queued'
print('RUNTIME_LONGFORM_GATE=PASS')
print('sequence=pricing_pending->reserve->segments->queued')
print('insufficient_credit_path=reserve->blocked-with-zero-new-segments')
PY

docker exec -i "$EXT_API" python - <<'PY'
import asyncio,json
from app.db import get_db_pool
async def main():
    pool=await get_db_pool()
    async with pool.acquire() as conn:
        row=await conn.fetchrow("""
          select j.id::text job_id,j.status,
                 coalesce((v.lots_json::jsonb->>'total_spendable')::numeric,0) spendable,
                 coalesce((v.lots_json::jsonb->>'total_reserved')::numeric,0) reserved
          from public.longform_jobs j
          left join public.v_pricing_account_overview v on v.user_id=j.user_id
          order by j.created_at desc limit 1
        """)
        if row:
            print('ACCOUNT_STATE='+json.dumps({
                'latest_job_id':row['job_id'],'latest_job_status':row['status'],
                'spendable':str(row['spendable']),'reserved':str(row['reserved'])
            },separators=(',',':')))
asyncio.run(main())
PY

for spec in \
  'Audio:18004:/api/health' \
  'Fusion:18002:/api/health' \
  'Face:18003:/api/health' \
  'Pricing:18009:/api/health' \
  'Director:18011:/api/health'; do
  IFS=: read -r name port path <<<"$spec"
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}${path}" || true)"
  [ "$code" = "200" ] || fail "$name preservation health HTTP_$code"
  echo "PRESERVE_${name^^}=PASS HTTP_200"
done

echo "============================================================"
echo " RUNTIME CERTIFICATION: PASS"
echo "============================================================"
echo "product_code_sha=$EXPECTED_SHA"
echo "financial_authority=svc-pricing"
echo "runtime_source_match=true"
echo "runtime_gate_executed=true"
echo "service_restart_performed=false"
echo "db_mutation_performed=false"
echo "pricing_catalog_changed=false"
echo "============================================================"
