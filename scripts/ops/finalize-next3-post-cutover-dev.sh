#!/usr/bin/env bash
set -Eeuo pipefail

WEB_SHA="${1:-}"
WEB_OPS_SHA="${2:-}"
EXPECTED_HOST="desifaces-dev"
WEB_DEPLOY="/tmp/deploy-next3-web-dev.sh"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host required"
[[ "$WEB_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact web application SHA required"
[[ "$WEB_OPS_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact web ops SHA required"

mapfile -t DBS < <(docker ps --format '{{.Names}}' | grep -E '^desifaces(-v3)?-db$' || true)
(( ${#DBS[@]} == 1 )) || fail "expected one running DB, found: ${DBS[*]:-none}"
[[ "${DBS[0]}" == "desifaces-db" ]] || fail "DB must be desifaces-db"

mapfile -t REDIS < <(docker ps --format '{{.Names}}' | grep -E '^desifaces(-v3)?-redis$' || true)
(( ${#REDIS[@]} == 1 )) || fail "expected one running Redis, found: ${REDIS[*]:-none}"
[[ "${REDIS[0]}" == "desifaces-redis" ]] || fail "Redis must be desifaces-redis"

SERVICES=(
  df-svc-pricing
  df-svc-core
  df-svc-face
  df-svc-face-worker
  df-svc-audio
  df-svc-audio-worker
  df-svc-fusion
  df-svc-fusion-worker
  df-svc-fusion-extension
  df-svc-fusion-extension-worker
  df-svc-fusion-extension-stitch-worker
  df-svc-dashboard
  df-svc-dashboard-worker
  df-svc-director
  df-svc-director-worker
)

for c in "${SERVICES[@]}"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "missing runtime container: $c"
  [[ "$(docker inspect -f '{{.State.Status}}' "$c")" == "running" ]] || fail "$c is not running"

  envtxt="$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}')"
  python3 - "$c" "$envtxt" <<'PY'
import sys
from urllib.parse import urlsplit

target=sys.argv[1]
raw=sys.argv[2]
env={}
for line in raw.splitlines():
    if "=" in line:
        k,v=line.split("=",1)
        env[k]=v

db=env.get("DATABASE_URL","")
redis=env.get("REDIS_URL","")
if db:
    u=urlsplit(db)
    if u.hostname != "desifaces-db" or u.path.lstrip("/") != "desifaces":
        raise SystemExit(f"{target}: invalid effective DATABASE_URL target")
if redis:
    u=urlsplit(redis)
    if u.hostname != "desifaces-redis":
        raise SystemExit(f"{target}: invalid effective REDIS_URL target")
print(f"RUNTIME_DATA_TARGETS {target}=PASS")
PY
done

echo "BACKEND_RUNTIME_DATA_TARGETS=PASS"

wait_http(){
  local url="$1" expected="$2" label="$3"
  local code="000"
  for i in $(seq 1 20); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 "$url" 2>/dev/null || true)"
    [[ "$code" == "$expected" ]] && { echo "$label=PASS"; return 0; }
    sleep 1
  done
  fail "$label unhealthy http=$code"
}

wait_http http://127.0.0.1:18009/api/health 200 PRICING
wait_http http://127.0.0.1:18000/ 200 CORE
wait_http http://127.0.0.1:18003/ 200 FACE
wait_http http://127.0.0.1:18004/ 200 AUDIO
wait_http http://127.0.0.1:18002/ 200 FUSION
wait_http http://127.0.0.1:18006/api/health 200 FUSION_EXTENSION
wait_http http://127.0.0.1:18005/ 200 DASHBOARD
wait_http http://127.0.0.1:18011/api/health 200 DIRECTOR

[[ "$(docker ps --filter 'label=com.docker.compose.service=svc-fusion-worker' --format '{{.Names}}' | wc -l)" -eq 1 ]] || fail "Fusion worker ownership is not singular"
FENV="$(docker inspect df-svc-fusion-worker --format '{{range .Config.Env}}{{println .}}{{end}}')"
grep -qx 'DF_FUSION_WORKER_CONCURRENCY=8' <<<"$FENV" || fail "Fusion worker concurrency != 8"
grep -qx 'DF_SYNC3_PROVIDER_CONCURRENCY=1' <<<"$FENV" || fail "Sync3 provider concurrency != 1"
grep -qx 'DF_SYNC3_CONCURRENCY_WAIT_SECONDS=900' <<<"$FENV" || fail "Sync3 wait != 900"
echo "BACKEND_RUNTIME_CERTIFICATION=PASS"

docker exec desifaces-db sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select exists(select 1 from public.v3_studio_workflows where workflow_id='\''2ef2b35b-f515-47da-964a-9c35669863fd'\''::uuid)::text || '\''|'\'' || exists(select 1 from public.v3_studio_stage_runs where stage_run_id='\''69b968f3-793e-433e-bdd9-03ec2afa43e8'\''::uuid)::text;"' \
  | grep -qx 'true|true' || fail "target workflow/stage missing"
echo "TARGET_WORKFLOW_INTEGRITY=PASS"

gh api "repos/prasshanthshankar-afk/desifaces_web/contents/scripts/ops/deploy-next3-web-dev.sh?ref=${WEB_OPS_SHA}" --jq .content \
  | base64 -d > "$WEB_DEPLOY"
chmod 700 "$WEB_DEPLOY"
bash -n "$WEB_DEPLOY"
echo "WEB_DEPLOY_SCRIPT=PASS"

NEXT3_SKIP_WEB_BUILD=1 bash "$WEB_DEPLOY" "$WEB_SHA"
echo "WEB_RUNTIME_CERTIFICATION=PASS"

echo "============================================================"
echo "NEXT3_POST_CUTOVER_FINALIZATION=PASS"
echo "web_sha=$WEB_SHA"
echo "db=desifaces-db/desifaces"
echo "redis=desifaces-redis"
echo "production_touch=NONE"
echo "next_url=https://dev-api.desifaces.ai/app/multi-person?story_id=67b9a735-79e2-5f9b-9271-4ddfbce49a07"
echo "============================================================"
