#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_SHA="${1:-}"
WEB_SHA="${2:-}"
SKIP_BUILD="${NEXT3_SKIP_BUILD:-0}"
EXPECTED_HOST="desifaces-dev"
BACKEND_REPO="${BACKEND_REPO:-/home/azureuser/workspace/desifaces-v3}"
WEB_REPO="${WEB_REPO:-/home/azureuser/workspace/desifaces-web}"
ENV_FILE="/home/azureuser/workspace/desifaces-v3/infra/.env"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE="/home/azureuser/.local/state/next3-fresh-${STAMP}"
BWT="${STATE}/backend"
WWT="${STATE}/web"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host required"
[[ "$BACKEND_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact backend SHA required"
[[ "$WEB_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact web SHA required"
[[ -f "$ENV_FILE" ]] || fail "canonical env missing: $ENV_FILE"
[[ -d "$BACKEND_REPO/.git" || -f "$BACKEND_REPO/.git" ]] || fail "backend repo missing"
[[ -d "$WEB_REPO/.git" || -f "$WEB_REPO/.git" ]] || fail "web repo missing"

mkdir -p "$STATE"

mapfile -t DBS < <(docker ps --format '{{.Names}}' | grep -E '^desifaces(-v3)?-db$' || true)
(( ${#DBS[@]} == 1 )) || fail "expected one running DB, found: ${DBS[*]:-none}"
[[ "${DBS[0]}" == "desifaces-db" ]] || fail "DB must be desifaces-db"

docker exec desifaces-db sh -lc   'test "$POSTGRES_DB" = desifaces && psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d desifaces -Atc "select current_database();"'   | grep -qx desifaces || fail "canonical DB desifaces unavailable"

mapfile -t REDIS < <(docker ps --format '{{.Names}}' | grep -E '^desifaces(-v3)?-redis$' || true)
(( ${#REDIS[@]} == 1 )) || fail "expected one running Redis, found: ${REDIS[*]:-none}"
[[ "${REDIS[0]}" == "desifaces-redis" ]] || fail "Redis must be desifaces-redis"
[[ "$(docker exec desifaces-redis redis-cli PING)" == "PONG" ]] || fail "Redis unavailable"

echo "INFRASTRUCTURE_PREFLIGHT=PASS"
echo "env=$ENV_FILE"
echo "db=desifaces-db/desifaces"
echo "redis=desifaces-redis"

git -C "$BACKEND_REPO" fetch --no-tags origin "$BACKEND_SHA"
git -C "$WEB_REPO" fetch --no-tags origin "$WEB_SHA"
git -C "$BACKEND_REPO" worktree remove --force "$BWT" >/dev/null 2>&1 || true
git -C "$WEB_REPO" worktree remove --force "$WWT" >/dev/null 2>&1 || true
rm -rf "$BWT" "$WWT"
git -C "$BACKEND_REPO" worktree add --detach "$BWT" "$BACKEND_SHA"
git -C "$WEB_REPO" worktree add --detach "$WWT" "$WEB_SHA"
[[ "$(git -C "$BWT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend SHA mismatch"
[[ "$(git -C "$WWT" rev-parse HEAD)" == "$WEB_SHA" ]] || fail "web SHA mismatch"

echo "EXACT_SOURCE=PASS"

export DESIFACES_RUNTIME_ENV_FILE="$ENV_FILE"
compose(){
  docker compose     --env-file "$ENV_FILE"     -f "$BWT/docker-compose.yml"     -f "$BWT/docker-compose.v3.yml"     -p desifaces     --profile v3-execution     --profile v3-orchestration     "$@"
}

BUILD_SERVICES=(
  svc-core
  svc-pricing
  svc-face
  svc-face-worker
  svc-audio
  svc-audio-worker
  svc-fusion
  svc-fusion-worker
  svc-fusion-extension
  svc-fusion-extension-worker
  svc-fusion-extension-stitch-worker
  svc-dashboard
  svc-dashboard-worker
  svc-director
  svc-director-worker
)

if [[ "$SKIP_BUILD" == "1" ]]; then
  echo "FRESH_BACKEND_IMAGES_BUILD=REUSED"
  echo "FRESH_WEB_IMAGE_BUILD=REUSED"
else
  compose build "${BUILD_SERVICES[@]}"
  echo "FRESH_BACKEND_IMAGES_BUILD=PASS"

  docker build -t "desifaces-web-next3:${WEB_SHA}" "$WWT/web"
  echo "FRESH_WEB_IMAGE_BUILD=PASS"
fi

compose run --rm --no-deps --entrypoint python svc-director - <<'PY'
import inspect
from app.studio_e2e_routes import fusion_execution
from app.fusion_execution_background_read import BackgroundFinalizedParallelSceneFusionExecutionService
from app.fusion_execution_parent_pricing import ParentPricedSceneFusionExecutionService
assert isinstance(fusion_execution, BackgroundFinalizedParallelSceneFusionExecutionService)
source = inspect.getsource(ParentPricedSceneFusionExecutionService.preview)
assert "if required_turn_ids:" in source
assert "fusion_preserved_child_lineage_mismatch" in source
print("DIRECTOR_CONTRACT=PASS")
PY

compose run --rm --no-deps --entrypoint python svc-fusion-extension - <<'PY'
from app.api.routes.v3_scene_stitch import _effective_scene_stitch_mode
assert _effective_scene_stitch_mode("xfade", "shared_scene") == "hard_cut"
assert _effective_scene_stitch_mode("xfade", "ordered_speaker_shots") == "xfade"
print("SHARED_SCENE_STITCH_CONTRACT=PASS")
PY

compose run --rm --no-deps --entrypoint python svc-fusion - <<'PY'
from app.services.providers.sync3_adapter import _provider_concurrency_limit, _provider_wait_seconds
assert _provider_concurrency_limit() == 1
assert _provider_wait_seconds() == 900.0
print("SYNC3_CONCURRENCY_CONTRACT=PASS")
PY

docker exec -i desifaces-db sh -lc   'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'   < "$BWT/migrations/2026_09_26_next3_multi_person_integrity.sql"
echo "NEXT3_DB_INTEGRITY_MIGRATION=PASS"

declare -A TARGET=(
  [svc-core]="df-svc-core"
  [svc-pricing]="df-svc-pricing"
  [svc-face]="df-svc-face"
  [svc-face-worker]="df-svc-face-worker"
  [svc-audio]="df-svc-audio"
  [svc-audio-worker]="df-svc-audio-worker"
  [svc-fusion]="df-svc-fusion"
  [svc-fusion-worker]="df-svc-fusion-worker"
  [svc-fusion-extension]="df-svc-fusion-extension"
  [svc-fusion-extension-worker]="df-svc-fusion-extension-worker"
  [svc-fusion-extension-stitch-worker]="df-svc-fusion-extension-stitch-worker"
  [svc-dashboard]="df-svc-dashboard"
  [svc-dashboard-worker]="df-svc-dashboard-worker"
  [svc-director]="df-svc-director"
  [svc-director-worker]="df-svc-director-worker"
)

SERVICES=(
  svc-pricing
  svc-core
  svc-face
  svc-face-worker
  svc-audio
  svc-audio-worker
  svc-fusion
  svc-fusion-worker
  svc-fusion-extension
  svc-fusion-extension-worker
  svc-fusion-extension-stitch-worker
  svc-dashboard
  svc-dashboard-worker
  svc-director
  svc-director-worker
)

declare -A ROLLBACK=()
declare -A OLD_RESTART=()
declare -A OLD_RUNNING=()
CUTOVER_STARTED=0

rollback_all(){
  local rc=$?
  set +e
  if [[ "$CUTOVER_STARTED" == "1" ]]; then
    echo "AUTOMATIC_ROLLBACK=START"
    for service in "${SERVICES[@]}"; do
      target="${TARGET[$service]}"
      docker rm -f "$target" >/dev/null 2>&1 || true
    done
    for service in "${SERVICES[@]}"; do
      target="${TARGET[$service]}"
      rb="${ROLLBACK[$service]:-}"
      [[ -n "$rb" ]] || continue
      if docker inspect "$rb" >/dev/null 2>&1; then
        docker rename "$rb" "$target" >/dev/null 2>&1 || true
        docker update --restart="${OLD_RESTART[$service]:-unless-stopped}" "$target" >/dev/null 2>&1 || true
        if [[ "${OLD_RUNNING[$service]:-false}" == "true" ]]; then
          docker start "$target" >/dev/null 2>&1 || true
        fi
      fi
    done
    echo "AUTOMATIC_ROLLBACK=COMPLETE"
  fi
  exit "$rc"
}
trap rollback_all ERR

CUTOVER_STARTED=1
for service in "${SERVICES[@]}"; do
  target="${TARGET[$service]}"
  if docker inspect "$target" >/dev/null 2>&1; then
    rb="${target}-rollback-${STAMP}"
    ROLLBACK[$service]="$rb"
    OLD_RESTART[$service]="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$target")"
    OLD_RUNNING[$service]="$(docker inspect -f '{{.State.Running}}' "$target")"
    docker update --restart=no "$target" >/dev/null 2>&1 || true
    docker stop "$target" >/dev/null 2>&1 || true
    docker rename "$target" "$rb"
    echo "ROLLBACK_SNAPSHOT $target -> $rb"
  else
    ROLLBACK[$service]=""
    OLD_RESTART[$service]="unless-stopped"
    OLD_RUNNING[$service]="false"
  fi

  compose up -d --no-deps --no-build "$service"
  [[ "$(docker inspect -f '{{.State.Status}}' "$target" 2>/dev/null || true)" == "running" ]]     || fail "$target failed to start"
  echo "CUTOVER $target=PASS"
done

wait_http(){
  local url="$1" expected="$2" label="$3"
  local code="000"
  for i in $(seq 1 40); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 "$url" 2>/dev/null || true)"
    echo "$label wait=$i http=$code"
    [[ "$code" == "$expected" ]] && return 0
    sleep 1
  done
  return 1
}

wait_http http://127.0.0.1:18009/api/health 200 PRICING || fail "pricing unhealthy"
wait_http http://127.0.0.1:18000/ 200 CORE || fail "core unhealthy"
wait_http http://127.0.0.1:18003/ 200 FACE || fail "face unhealthy"
wait_http http://127.0.0.1:18004/ 200 AUDIO || fail "audio unhealthy"
wait_http http://127.0.0.1:18002/ 200 FUSION || fail "fusion unhealthy"
wait_http http://127.0.0.1:18006/api/health 200 FUSION_EXTENSION || fail "fusion extension unhealthy"
wait_http http://127.0.0.1:18005/ 200 DASHBOARD || fail "dashboard unhealthy"
wait_http http://127.0.0.1:18011/api/health 200 DIRECTOR || fail "director unhealthy"

for service in "${SERVICES[@]}"; do
  target="${TARGET[$service]}"
  envtxt="$(docker inspect "$target" --format '{{range .Config.Env}}{{println .}}{{end}}')"
  python3 - "$target" "$envtxt" <<'PY'
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
        raise SystemExit(f"{target}: DATABASE_URL points to {u.hostname}/{u.path.lstrip('/')}")
if redis:
    u=urlsplit(redis)
    if u.hostname != "desifaces-redis":
        raise SystemExit(f"{target}: REDIS_URL points to {u.hostname}")
print(f"RUNTIME_DATA_TARGETS {target}=PASS")
PY
done

[[ "$(docker ps --filter 'label=com.docker.compose.service=svc-fusion-worker' --format '{{.Names}}' | wc -l)" -eq 1 ]]   || fail "Fusion worker ownership is not singular"

FENV="$(docker inspect df-svc-fusion-worker --format '{{range .Config.Env}}{{println .}}{{end}}')"
grep -qx 'DF_FUSION_WORKER_CONCURRENCY=8' <<<"$FENV" || fail "Fusion worker concurrency != 8"
grep -qx 'DF_SYNC3_PROVIDER_CONCURRENCY=1' <<<"$FENV" || fail "Sync3 provider concurrency != 1"
grep -qx 'DF_SYNC3_CONCURRENCY_WAIT_SECONDS=900' <<<"$FENV" || fail "Sync3 wait != 900"

echo "BACKEND_RUNTIME_CERTIFICATION=PASS"

WEB_DEPLOY="$WWT/scripts/ops/deploy-next3-web-dev.sh"
[[ -f "$WEB_DEPLOY" ]] || fail "web deployment script missing"
chmod 700 "$WEB_DEPLOY"
WEB_REPO_ROOT="$WEB_REPO" bash "$WEB_DEPLOY" "$WEB_SHA"

echo "WEB_RUNTIME_CERTIFICATION=PASS"

docker exec desifaces-db sh -lc   'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select exists(select 1 from public.v3_studio_workflows where workflow_id='''2ef2b35b-f515-47da-964a-9c35669863fd'''::uuid)::text || '''|''' || exists(select 1 from public.v3_studio_stage_runs where stage_run_id='''69b968f3-793e-433e-bdd9-03ec2afa43e8'''::uuid)::text;"'   | grep -qx 'true|true' || fail "target workflow/stage missing"

echo "TARGET_WORKFLOW_INTEGRITY=PASS"

trap - ERR

echo "============================================================"
echo "NEXT3_FRESH_DEV_DEPLOY=PASS"
echo "backend_sha=$BACKEND_SHA"
echo "web_sha=$WEB_SHA"
echo "db=desifaces-db/desifaces"
echo "redis=desifaces-redis"
echo "production_touch=NONE"
echo "next_url=https://dev-api.desifaces.ai/app/multi-person?story_id=67b9a735-79e2-5f9b-9271-4ddfbce49a07"
echo "============================================================"
