#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_SHA="${1:-}"
WEB_SHA="${2:-}"
EXPECTED_HOST="desifaces-dev"
BACKEND_REPO="${BACKEND_REPO:-/home/azureuser/workspace/desifaces-v3}"
WEB_REPO="${WEB_REPO:-/home/azureuser/workspace/desifaces-web}"
LIVE_ENV="${LIVE_ENV:-/home/azureuser/workspace/desifaces-v3/infra/.env}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE="/home/azureuser/.local/state/next3-e2e-${STAMP}"
BWT="${STATE}/backend"
WWT="${STATE}/web"
LOG="${STATE}/certification.log"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "DEV host guard failed"
[[ "$BACKEND_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact backend SHA required"
[[ "$WEB_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact web SHA required"
[[ -f "$LIVE_ENV" ]] || fail "missing live DEV env: $LIVE_ENV"
[[ -d "$BACKEND_REPO/.git" || -f "$BACKEND_REPO/.git" ]] || fail "backend repo missing"
[[ -d "$WEB_REPO/.git" || -f "$WEB_REPO/.git" ]] || fail "web repo missing"

mkdir -p "$STATE"
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo " desifaces NEXT3 — END-TO-END DEV IMPLEMENTATION"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " host=$(hostname -s)"
echo " production_touch=NONE"
echo "============================================================"

# ---------------------------------------------------------------------------
# 1. Materialize exact source SHAs.
# ---------------------------------------------------------------------------
git -C "$BACKEND_REPO" fetch --no-tags origin "$BACKEND_SHA"
git -C "$WEB_REPO" fetch --no-tags origin "$WEB_SHA"

git -C "$BACKEND_REPO" worktree remove --force "$BWT" >/dev/null 2>&1 || true
git -C "$WEB_REPO" worktree remove --force "$WWT" >/dev/null 2>&1 || true
rm -rf "$BWT" "$WWT"

git -C "$BACKEND_REPO" worktree add --detach "$BWT" "$BACKEND_SHA"
git -C "$WEB_REPO" worktree add --detach "$WWT" "$WEB_SHA"

[[ "$(git -C "$BWT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend worktree SHA mismatch"
[[ "$(git -C "$WWT" rev-parse HEAD)" == "$WEB_SHA" ]] || fail "web worktree SHA mismatch"
echo "EXACT_SOURCE=PASS"

compose(){
  docker compose     --env-file "$LIVE_ENV"     -f "$BWT/docker-compose.yml"     -f "$BWT/docker-compose.v3.yml"     -p desifaces     --profile v3-execution     --profile v3-orchestration     "$@"
}

# ---------------------------------------------------------------------------
# 2. Build every NEXT3-owned runtime before touching live containers.
# ---------------------------------------------------------------------------
compose build   svc-director   svc-director-worker   svc-face   svc-face-worker   svc-fusion   svc-fusion-worker   svc-fusion-extension   svc-fusion-extension-stitch-worker

echo "BACKEND_CANDIDATE_BUILD=PASS"

docker build   -t "desifaces-web-next3:${WEB_SHA}"   "$WWT/web"

echo "WEB_CANDIDATE_BUILD=PASS"

# ---------------------------------------------------------------------------
# 3. Candidate contract gates: no live mutation.
# ---------------------------------------------------------------------------
compose run --rm --no-deps --entrypoint python svc-director - <<'PY'
import inspect
from app.studio_e2e_routes import fusion_execution
from app.fusion_execution_background_read import BackgroundFinalizedParallelSceneFusionExecutionService
from app.fusion_execution_parent_pricing import ParentPricedSceneFusionExecutionService
from app.fusion_execution_parallel_dispatch import ParallelOrphanReconciledParentPricedSceneFusionExecutionService

assert isinstance(fusion_execution, BackgroundFinalizedParallelSceneFusionExecutionService)
assert isinstance(fusion_execution, ParallelOrphanReconciledParentPricedSceneFusionExecutionService)
source = inspect.getsource(ParentPricedSceneFusionExecutionService.preview)
assert "if required_turn_ids:" in source
assert "fusion_preserved_child_lineage_mismatch" in source
print("DIRECTOR_BACKGROUND_RUNTIME_CONTRACT=PASS")
print("STITCH_ONLY_SKIPS_UPSTREAM_GENERATION_INPUTS=PASS")
print("PRESERVED_CHILD_LINEAGE_GUARD=PASS")
PY

compose run --rm --no-deps --entrypoint python svc-fusion-extension - <<'PY'
from app.api.routes.v3_scene_stitch import _effective_scene_stitch_mode, _media_storage_location
assert _effective_scene_stitch_mode("xfade", "shared_scene") == "hard_cut"
assert _effective_scene_stitch_mode(None, "shared_scene") == "hard_cut"
assert _effective_scene_stitch_mode("xfade", "ordered_speaker_shots") == "xfade"
c,b = _media_storage_location(
    "https://account.blob.core.windows.net/face-output/shared/group.png?sig=old",
    {},
)
assert c == "face-output" and b == "shared/group.png"
print("SHARED_SCENE_HARD_CUT_CONTRACT=PASS")
print("SHARED_MEDIA_FALLBACK_CONTRACT=PASS")
PY

compose run --rm --no-deps --entrypoint python svc-face - <<'PY'
from app.main import app
paths={getattr(route,"path","") for route in app.routes}
assert "/api/face/config/countries" in paths
assert "/api/face/assets/{media_id}/read-url" in paths
print("FACE_COUNTRY_CATALOG_ROUTE=PASS")
print("FACE_DURABLE_MEDIA_READ_ROUTE=PASS")
PY

compose run --rm --no-deps --entrypoint python svc-fusion - <<'PY'
from app.services.providers.sync3_adapter import _provider_concurrency_limit, _provider_wait_seconds
assert _provider_concurrency_limit() == 1
assert _provider_wait_seconds() == 900.0
print("SYNC3_PROVIDER_CONCURRENCY=1")
print("SYNC3_CONCURRENCY_WAIT_SECONDS=900")
PY

echo "BACKEND_CANDIDATE_CONTRACTS=PASS"

# ---------------------------------------------------------------------------
# 4. Database integrity migration. No container restart and no destructive cleanup.
# ---------------------------------------------------------------------------
mapfile -t DB_RUNNING < <(
  docker ps     --filter 'label=com.docker.compose.service=desifaces-db'     --format '{{.Names}}'
)
if (( ${#DB_RUNNING[@]} == 0 )); then
  for candidate in desifaces-db desifaces-v3-db; do
    if docker inspect "$candidate" >/dev/null 2>&1       && [[ "$(docker inspect -f '{{.State.Status}}' "$candidate")" == "running" ]]; then
      DB_RUNNING=("$candidate")
      break
    fi
  done
fi
(( ${#DB_RUNNING[@]} == 1 )) || fail "expected exactly one running desifaces DB, found: ${DB_RUNNING[*]:-none}"
DB="${DB_RUNNING[0]}"

docker exec -i "$DB" sh -lc   'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'   < "$BWT/migrations/2026_09_26_next3_multi_person_integrity.sql"

echo "NEXT3_DB_INTEGRITY_MIGRATION=PASS"

# ---------------------------------------------------------------------------
# 5. Preserve current runtimes for automatic rollback, then cut over only
#    NEXT3-owned services. All new container names are stable and non-versioned.
# ---------------------------------------------------------------------------
declare -A TARGET=(
  [svc-face]="df-svc-face"
  [svc-fusion]="df-svc-fusion"
  [svc-fusion-extension]="df-svc-fusion-extension"
  [svc-director]="df-svc-director"
  [svc-face-worker]="df-svc-face-worker"
  [svc-fusion-worker]="df-svc-fusion-worker"
  [svc-fusion-extension-stitch-worker]="df-svc-fusion-extension-stitch-worker"
  [svc-director-worker]="df-svc-director-worker"
)
SERVICES=(
  svc-face
  svc-fusion
  svc-fusion-extension
  svc-director
  svc-face-worker
  svc-fusion-worker
  svc-fusion-extension-stitch-worker
  svc-director-worker
)

declare -A OLD_NAME
declare -A ROLLBACK_NAME
declare -A OLD_RESTART
declare -A OLD_RUNNING
CUTOVER_STARTED=0

preserve_live(){
  local service="$1"
  local target="${TARGET[$service]}"
  local current=""
  local canonical_conflict=""

  mapfile -t running < <(
    docker ps       --filter "label=com.docker.compose.service=$service"       --format '{{.Names}}'
  )
  if (( ${#running[@]} > 1 )); then
    fail "duplicate running service detected for $service: ${running[*]}"
  fi
  if (( ${#running[@]} == 1 )); then
    current="${running[0]}"
  fi

  if docker inspect "$target" >/dev/null 2>&1 && [[ "$target" != "$current" ]]; then
    canonical_conflict="${target}-legacy-${STAMP}"
    local n=0
    while docker inspect "$canonical_conflict" >/dev/null 2>&1; do
      n=$((n+1))
      canonical_conflict="${target}-legacy-${STAMP}-${n}"
    done
    docker rename "$target" "$canonical_conflict"
    echo "PRESERVED_STALE_CANONICAL $target -> $canonical_conflict"
  fi

  if [[ -n "$current" ]]; then
    local rollback="${target}-rollback-${STAMP}"
    local n=0
    while docker inspect "$rollback" >/dev/null 2>&1; do
      n=$((n+1))
      rollback="${target}-rollback-${STAMP}-${n}"
    done

    OLD_NAME["$service"]="$current"
    ROLLBACK_NAME["$service"]="$rollback"
    OLD_RESTART["$service"]="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$current")"
    OLD_RUNNING["$service"]="true"

    docker update --restart=no "$current" >/dev/null
    docker stop "$current" >/dev/null
    docker rename "$current" "$rollback"
    echo "ROLLBACK_SNAPSHOT $service $current -> $rollback"
  else
    OLD_NAME["$service"]=""
    ROLLBACK_NAME["$service"]=""
    OLD_RESTART["$service"]="no"
    OLD_RUNNING["$service"]="false"
  fi
}

rollback_all(){
  local rc=$?
  set +e
  echo "============================================================"
  echo " NEXT3 AUTOMATIC ROLLBACK"
  echo "============================================================"

  for service in "${SERVICES[@]}"; do
    target="${TARGET[$service]}"
    docker rm -f "$target" >/dev/null 2>&1 || true
  done

  for service in "${SERVICES[@]}"; do
    rollback="${ROLLBACK_NAME[$service]:-}"
    original="${OLD_NAME[$service]:-}"
    [[ -n "$rollback" && -n "$original" ]] || continue
    if docker inspect "$rollback" >/dev/null 2>&1; then
      docker rename "$rollback" "$original" >/dev/null 2>&1 || true
      policy="${OLD_RESTART[$service]:-no}"
      docker update --restart="$policy" "$original" >/dev/null 2>&1 || true
      [[ "${OLD_RUNNING[$service]:-false}" == "true" ]] && docker start "$original" >/dev/null 2>&1 || true
    fi
  done
  echo "NEXT3_AUTOMATIC_ROLLBACK=COMPLETE"
  exit "$rc"
}

trap rollback_all ERR
CUTOVER_STARTED=1

for service in "${SERVICES[@]}"; do
  preserve_live "$service"
  compose up -d --no-deps --force-recreate "$service"
  target="${TARGET[$service]}"
  for _ in $(seq 1 30); do
    [[ "$(docker inspect -f '{{.State.Status}}' "$target" 2>/dev/null || true)" == "running" ]] && break
    sleep 1
  done
  [[ "$(docker inspect -f '{{.State.Status}}' "$target" 2>/dev/null || true)" == "running" ]]     || fail "$service failed to start as $target"
  echo "CUTOVER $service -> $target PASS"
done

# ---------------------------------------------------------------------------
# 6. Live backend health and ownership gates.
# ---------------------------------------------------------------------------
wait_http(){
  local url="$1" expected="${2:-200}" label="$3"
  local code=000
  for i in $(seq 1 45); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 "$url" 2>/dev/null || true)"
    echo "$label wait=$i http=$code"
    [[ "$code" == "$expected" ]] && return 0
    sleep 1
  done
  return 1
}

wait_http http://127.0.0.1:18003/ 200 FACE_API || fail "Face API unhealthy"
wait_http http://127.0.0.1:18002/ 200 FUSION_API || fail "Fusion API unhealthy"
wait_http http://127.0.0.1:18006/api/health 200 FUSION_EXTENSION_API || fail "Fusion Extension unhealthy"
wait_http http://127.0.0.1:18011/api/health 200 DIRECTOR_API || fail "Director unhealthy"

COUNTRY_COUNT="$(
  curl -fsS --max-time 5 'http://127.0.0.1:18003/api/face/config/countries?language=en'   | python3 -c 'import json,sys; x=json.load(sys.stdin); print(len(x if isinstance(x,list) else x.get("items",[])))'
)"
[[ "$COUNTRY_COUNT" =~ ^[0-9]+$ && "$COUNTRY_COUNT" -gt 0 ]] || fail "Face country catalog empty"
echo "FACE_COUNTRY_CATALOG_LIVE=PASS count=$COUNTRY_COUNT"

FUSION_ENV="$(
  docker inspect df-svc-fusion-worker     --format '{{range .Config.Env}}{{println .}}{{end}}'
)"
grep -qx 'DF_FUSION_WORKER_CONCURRENCY=8' <<<"$FUSION_ENV" || fail "Fusion worker concurrency != 8"
grep -qx 'DF_SYNC3_PROVIDER_CONCURRENCY=1' <<<"$FUSION_ENV" || fail "Sync3 provider concurrency != 1"
grep -qx 'DF_SYNC3_CONCURRENCY_WAIT_SECONDS=900' <<<"$FUSION_ENV" || fail "Sync3 wait != 900"

[[ "$(
  docker ps     --filter 'label=com.docker.compose.service=svc-fusion-worker'     --format '{{.Names}}' | wc -l
)" -eq 1 ]] || fail "more than one Fusion worker is running"

docker exec -i df-svc-fusion-extension-stitch-worker python - <<'PY'
from app.api.routes.v3_scene_stitch import _effective_scene_stitch_mode
import os
assert _effective_scene_stitch_mode("xfade","shared_scene") == "hard_cut"
assert str(os.getenv("DF_V3_SCENE_COORDINATOR_ENABLED","")).lower() in {"1","true","yes","on"}
print("LIVE_SHARED_SCENE_HARD_CUT=PASS")
print("LIVE_BACKGROUND_SCENE_COORDINATOR=PASS")
PY

docker exec -i df-svc-director python - <<'PY'
from app.studio_e2e_routes import fusion_execution
from app.fusion_execution_background_read import BackgroundFinalizedParallelSceneFusionExecutionService, _background_enabled
assert isinstance(fusion_execution, BackgroundFinalizedParallelSceneFusionExecutionService)
assert _background_enabled() is True
print("LIVE_DIRECTOR_BACKGROUND_FINALIZATION=PASS")
PY

echo "BACKEND_LIVE_CERTIFICATION=PASS"

# ---------------------------------------------------------------------------
# 7. Deploy exact web SHA through its candidate/rollback path.
# ---------------------------------------------------------------------------
WEB_DEPLOY="$WWT/scripts/ops/deploy-next3-web-dev.sh"
[[ -f "$WEB_DEPLOY" ]] || fail "web deploy script missing"
chmod 700 "$WEB_DEPLOY"
WEB_REPO_ROOT="$WEB_REPO" bash "$WEB_DEPLOY" "$WEB_SHA"

WEB_ENV="$(
  docker inspect df-web-dev     --format '{{range .Config.Env}}{{println .}}{{end}}'
)"
grep -qx 'DIRECTOR_BASE_URL=http://svc-director:8011' <<<"$WEB_ENV" || fail "web Director route is not stable service alias"
grep -qx 'FACE_BASE_URL=http://svc-face:8003' <<<"$WEB_ENV" || fail "web Face route is not stable service alias"
grep -qx 'FUSION_EXTENSION_BASE_URL=http://svc-fusion-extension:8006' <<<"$WEB_ENV" || fail "web Fusion Extension route is not stable service alias"

echo "WEB_STABLE_SERVICE_ROUTING=PASS"

# ---------------------------------------------------------------------------
# 8. Relabel runtime names only. Docker rename does not restart containers.
#    This removes remaining V3 identifiers from desifaces container/process names.
# ---------------------------------------------------------------------------
mapfile -t VERSIONED < <(
  docker ps -a --format '{{.Names}}'   | grep -Ei '(^|_)df-v3-|(^|_)desifaces-v3-' || true
)

for old in "${VERSIONED[@]}"; do
  service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$old" 2>/dev/null || true)"
  [[ -n "$service" ]] || continue

  case "$service" in
    desifaces-db) canonical="desifaces-db" ;;
    desifaces-redis) canonical="desifaces-redis" ;;
    *) canonical="df-$service" ;;
  esac

  if docker inspect "$canonical" >/dev/null 2>&1; then
    canonical="${canonical}-legacy-${STAMP}"
    n=0
    while docker inspect "$canonical" >/dev/null 2>&1; do
      n=$((n+1))
      canonical="${canonical}-legacy-${STAMP}-${n}"
    done
  fi

  before="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}' "$old")"
  docker rename "$old" "$canonical"
  after="$(docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}' "$canonical")"
  [[ "$before" == "$after" ]] || fail "rename changed runtime state for $old"
  echo "CANONICALIZED_NAME $old -> $canonical"
done

remaining="$(
  docker ps -a --format '{{.Names}}'   | grep -Ei '(^|_)df-v3-|(^|_)desifaces-v3-' || true
)"
[[ -z "$remaining" ]] || fail "version-specific container names remain: $remaining"
echo "VERSIONED_CONTAINER_NAMES=0"

# ---------------------------------------------------------------------------
# 9. Database/data-lineage read-only certification.
# ---------------------------------------------------------------------------
docker exec -i df-svc-director python - <<'PY'
import os,asyncio,asyncpg

TARGET_STAGE="69b968f3-793e-433e-bdd9-03ec2afa43e8"

async def main():
    conn=await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        duplicate_active=await conn.fetchval("""
          select count(*) from (
            select stage_run_id
            from public.v3_studio_stage_attempts
            where state in ('dispatching','queued','running')
            group by stage_run_id having count(*) > 1
          ) x
        """)
        assert int(duplicate_active or 0) == 0

        duplicate_scene_media=await conn.fetchval("""
          select count(*) from (
            select meta_json->>'v3_studio_attempt_id'
            from public.media_assets
            where lifecycle_state='active'
              and kind='video'
              and meta_json->>'source_kind'='v3_scene_stitch'
              and nullif(meta_json->>'v3_studio_attempt_id','') is not null
            group by meta_json->>'v3_studio_attempt_id'
            having count(*) > 1
          ) x
        """)
        assert int(duplicate_scene_media or 0) == 0

        lineage=await conn.fetchval("""
          select count(*)
          from public.v3_studio_stage_inputs
          where stage_run_id=$1::uuid
            and input_role='approved_shared_scene_image'
        """, TARGET_STAGE)
        assert int(lineage or 0) == 1

        latest=await conn.fetchrow("""
          select attempt_no,state,metadata_json
          from public.v3_studio_stage_attempts
          where stage_run_id=$1::uuid
          order by attempt_no desc limit 1
        """, TARGET_STAGE)
        print("TARGET_LATEST_ATTEMPT="+str(latest["attempt_no"] if latest else "none"))
        print("TARGET_LATEST_STATE="+str(latest["state"] if latest else "none"))
        print("DB_ONE_ACTIVE_ATTEMPT=PASS")
        print("DB_ONE_FINAL_MEDIA_PER_ATTEMPT=PASS")
        print("DB_SHARED_SCENE_IMAGE_LINEAGE=PASS")
    finally:
        await conn.close()

asyncio.run(main())
PY

trap - ERR

echo "============================================================"
echo " NEXT3_E2E_DEV_IMPLEMENTATION=PASS"
echo " backend_sha=$BACKEND_SHA"
echo " web_sha=$WEB_SHA"
echo " active_director=df-svc-director"
echo " active_face=df-svc-face"
echo " active_fusion=df-svc-fusion"
echo " active_fusion_worker=df-svc-fusion-worker"
echo " active_stitch_worker=df-svc-fusion-extension-stitch-worker"
echo " production_touch=NONE"
echo " next_url=https://dev-api.desifaces.ai/app/multi-person?story_id=67b9a735-79e2-5f9b-9271-4ddfbce49a07"
echo "============================================================"
