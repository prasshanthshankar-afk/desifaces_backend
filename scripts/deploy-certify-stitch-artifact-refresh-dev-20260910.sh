#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REPO="/home/azureuser/workspace/desifaces-v3"
SOURCE_REF="00398530b6d8e5509bbac163aefca526400577b6"
WORKFLOW_ID="16099052-15b5-401f-a447-c5d989b7b8ad"
STAGE_ID="cbf4b76a-21ec-4b17-951a-e0674a6f247f"
EXPECTED_CHILDREN=9
ENVFILE="$REPO/infra/.env"
DB="desifaces-v3-db"
WORKER="df-v3-svc-fusion-extension-stitch-worker"
DIRECTOR="df-v3-svc-director"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WT="/tmp/desifaces-v3-artifact-refresh-${STAMP}"
NEW_IMAGE="desifaces-v3-stitch-artifact-refresh:${SOURCE_REF:0:12}"
ROLLBACK_TAG="desifaces-v3-stitch-artifact-refresh-rollback:${STAMP}"
OLD_CONFIG_IMAGE=""
CUTOVER=0

NON_TARGETS=(
  desifaces-v3-db desifaces-v3-redis
  df-v3-svc-director df-v3-svc-director-worker
  df-v3-svc-fusion df-v3-svc-fusion-worker
  df-v3-svc-face df-v3-svc-face-worker
  df-v3-svc-audio df-v3-svc-audio-worker
  df-v3-svc-fusion-extension df-v3-web
)

log(){ printf '%s\n' "$*"; }
fail(){ log "FAIL: $*"; exit 1; }

host="$(hostname -s)"
[[ "$host" == "$EXPECTED_HOST" ]] || fail "DEV host guard expected=$EXPECTED_HOST current=$host"
[[ -d "$REPO/.git" ]] || fail "backend repo missing: $REPO"
[[ -s "$ENVFILE" ]] || fail "DEV compose env missing: $ENVFILE"
command -v docker >/dev/null || fail "docker unavailable"
command -v git >/dev/null || fail "git unavailable"
docker inspect "$WORKER" >/dev/null 2>&1 || fail "$WORKER missing"
docker inspect "$DIRECTOR" >/dev/null 2>&1 || fail "$DIRECTOR missing"

compose(){
  (cd "$REPO" && docker compose --env-file "$ENVFILE" -f docker-compose.yml -f docker-compose.v3.yml --profile v3-execution "$@")
}

psqlq(){
  local sql="$1"
  docker exec -e DF_SQL="$sql" "$DB" sh -lc 'psql -X -A -t -F "|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_SQL"'
}

snapshot_non_targets(){
  local c
  for c in "${NON_TARGETS[@]}"; do
    if docker inspect "$c" >/dev/null 2>&1; then
      docker inspect "$c" --format '{{.Name}}|{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}'
    else
      printf '/%s|MISSING|MISSING|MISSING\n' "$c"
    fi
  done | sort
}

child_ids(){
  psqlq "
select j.id::text
from public.studio_jobs j
where j.studio_type='fusion' and (
 j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${STAGE_ID}' or
 j.payload_json #>> '{provider_options,billing_context,parent_longform_job_id}'='${STAGE_ID}' or
 j.payload_json #>> '{provider_options,billing_context,parent_job_id}'='${STAGE_ID}' or
 j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${STAGE_ID}' or
 j.payload_json #>> '{tags,billing_context,parent_longform_job_id}'='${STAGE_ID}' or
 j.payload_json #>> '{tags,billing_context,parent_job_id}'='${STAGE_ID}'
)
order by j.id;"
}

rollback(){
  local rc=$?
  if [[ $rc -ne 0 && "$CUTOVER" -eq 1 && -n "$OLD_CONFIG_IMAGE" ]] && docker image inspect "$ROLLBACK_TAG" >/dev/null 2>&1; then
    log ""
    log "===== AUTOMATIC STITCH-WORKER ROLLBACK ====="
    docker tag "$ROLLBACK_TAG" "$OLD_CONFIG_IMAGE" >/dev/null 2>&1 || true
    compose up -d --no-deps --no-build --force-recreate svc-fusion-extension-stitch-worker >/dev/null 2>&1 || true
    log "STITCH_WORKER_ROLLBACK=ATTEMPTED"
  fi
  if [[ -d "$WT" ]]; then
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT" || true
  fi
  exit "$rc"
}
trap rollback EXIT

log "============================================================"
log " desifaces DEV — STITCH ARTIFACT URL REFRESH DEPLOYMENT"
log "============================================================"
log "environment=DEV_ONLY"
log "source_ref=$SOURCE_REF"
log "workflow_id=$WORKFLOW_ID"
log "stage_run_id=$STAGE_ID"
log "child_rerender=FORBIDDEN"
log "provider_generation=NONE"
log "target_runtime=STITCH_WORKER_ONLY"
log "production_touch=NONE"

log ""
log "===== 1. DEV COMPOSE / SOURCE GATE ====="
compose config >/dev/null
log "COMPOSE_ENV_INTERPOLATION=PASS"
git -C "$REPO" fetch --quiet origin "$SOURCE_REF"
git -C "$REPO" worktree add --detach "$WT" "$SOURCE_REF" >/dev/null
[[ "$(git -C "$WT" rev-parse HEAD)" == "$SOURCE_REF" ]] || fail "immutable source mismatch"
python3 -m py_compile \
  "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_artifact_refresh.py" \
  "$WT/services/svc-fusion-extension/app/app/workers/stitch_worker.py"
grep -q 'v3_scene_artifact_refresh import v3_scene_coordinator_loop' \
  "$WT/services/svc-fusion-extension/app/app/workers/stitch_worker.py" || fail "stitch worker does not install artifact refresh"
grep -q 'svc-fusion-full-status-artifact' \
  "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_artifact_refresh.py" || fail "fresh artifact contract missing"
grep -q 'client.get(f"/jobs/{job_id}")' \
  "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_artifact_refresh.py" || fail "full Fusion job refresh missing"
log "FRESH_ARTIFACT_SOURCE_CONTRACT=PASS"

log ""
log "===== 2. FAILED SCENE / PRESERVED CHILDREN GATE ====="
scene="$(psqlq "
select s.state::text,
       coalesce(s.metadata_json #>> '{aspect_ratio}',''),
       coalesce(s.metadata_json #>> '{fusion_parent_pricing,state}',''),
       a.state::text,
       coalesce(a.error_code::text,''),
       coalesce(a.error_message::text,''),
       coalesce(jsonb_array_length(a.metadata_json->'children'),0)::text
from public.v3_studio_stage_runs s
join lateral (
 select state,error_code,error_message,metadata_json
 from public.v3_studio_stage_attempts
 where stage_run_id=s.stage_run_id order by attempt_no desc limit 1
) a on true
where s.stage_run_id='${STAGE_ID}'::uuid and s.workflow_id='${WORKFLOW_ID}'::uuid;")"
[[ -n "$scene" ]] || fail "target Scene missing"
IFS='|' read -r scene_state aspect parent_state attempt_state error_code error_message recorded_children <<<"$scene"
log "scene_state=$scene_state"
log "aspect_ratio=$aspect"
log "parent_pricing_state=$parent_state"
log "attempt_state=$attempt_state"
log "attempt_error_code=${error_code:-NONE}"
[[ "$scene_state" == "failed" ]] || fail "expected failed Scene after stale-SAS stitch, got $scene_state"
[[ "$aspect" == "16:9" ]] || fail "expected 16:9, got ${aspect:-missing}"
[[ "$parent_state" == "released" ]] || fail "expected released parent reservation, got ${parent_state:-missing}"
[[ "$attempt_state" == "failed" ]] || fail "expected failed attempt, got $attempt_state"
[[ "$error_message" == *"HTTP Error 403"* || "$error_message" == *"authenticate the request"* ]] || fail "latest failure is not the proven stale-SAS stitch failure"

before_ids="$(child_ids)"
before_count="$(printf '%s\n' "$before_ids" | sed '/^$/d' | wc -l | tr -d ' ')"
[[ "$before_count" -eq "$EXPECTED_CHILDREN" ]] || fail "expected $EXPECTED_CHILDREN child jobs, got $before_count"
summary="$(psqlq "
select count(*)::text,
       count(*) filter(where status='succeeded')::text,
       count(*) filter(where status in ('queued','processing','running','submitted','pending'))::text,
       count(*) filter(where status in ('failed','cancelled','canceled','blocked'))::text
from public.studio_jobs
where id=any(string_to_array('$(printf '%s' "$before_ids" | paste -sd, -)',',')::uuid[]);")"
IFS='|' read -r total succeeded active failed <<<"$summary"
log "video_children_total=$total"
log "video_children_succeeded=$succeeded"
log "video_children_active=$active"
log "video_children_failed=$failed"
[[ "$succeeded" -eq "$EXPECTED_CHILDREN" && "$active" -eq 0 && "$failed" -eq 0 ]] || fail "preserved Fusion videos are not all successful"
log "PRESERVED_9_VIDEO_CHILDREN=PASS"

log ""
log "===== 3. DIRECTOR STITCH-ONLY RECOVERY CONTRACT ====="
docker exec "$DIRECTOR" python - <<'PY'
import json, urllib.request
paths=(json.load(urllib.request.urlopen('http://127.0.0.1:8011/openapi.json',timeout=8)).get('paths') or {})
required='/api/director/studio-workflows/{workflow_id}/fusion-stages/{stage_run_id}/retry-stitch'
assert required in paths, required
print('DIRECTOR_RETRY_STITCH_ROUTE=PASS')
PY
docker exec "$DIRECTOR" sh -lc \
  'grep -q "install_preserved_child_url_refresh" /app/app/fusion_execution_runtime.py && grep -q "status_full" /app/app/fusion_execution_preserved_url_refresh.py' \
  || fail "Director runtime lacks preserved-child fresh URL retry guard"
log "DIRECTOR_PRESERVED_URL_REFRESH_RUNTIME=PASS"

before_non_targets="$(snapshot_non_targets)"

log ""
log "===== 4. BUILD IMMUTABLE STITCH-WORKER IMAGE ====="
docker build --pull=false \
  -t "$NEW_IMAGE" \
  -f "$WT/services/svc-fusion-extension/app/Dockerfile" \
  "$WT"
docker run --rm --entrypoint sh "$NEW_IMAGE" -lc \
  'test -s /app/app/workers/v3_scene_artifact_refresh.py && grep -q "v3_scene_artifact_refresh import v3_scene_coordinator_loop" /app/app/workers/stitch_worker.py && grep -q "svc-fusion-full-status-artifact" /app/app/workers/v3_scene_artifact_refresh.py'
log "STITCH_ARTIFACT_REFRESH_IMAGE=PASS"

log ""
log "===== 5. STITCH-WORKER-ONLY CUTOVER ====="
old_image_id="$(docker inspect "$WORKER" --format '{{.Image}}')"
OLD_CONFIG_IMAGE="$(docker inspect "$WORKER" --format '{{.Config.Image}}')"
[[ -n "$old_image_id" && -n "$OLD_CONFIG_IMAGE" ]] || fail "unable to capture rollback image"
[[ "$OLD_CONFIG_IMAGE" != sha256:* ]] || fail "worker configured image is not tag-addressable: $OLD_CONFIG_IMAGE"
docker tag "$old_image_id" "$ROLLBACK_TAG"
docker tag "$NEW_IMAGE" "$OLD_CONFIG_IMAGE"
compose up -d --no-deps --no-build --force-recreate svc-fusion-extension-stitch-worker
CUTOVER=1

for i in $(seq 1 45); do
  [[ "$(docker inspect "$WORKER" --format '{{.State.Status}}' 2>/dev/null || true)" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect "$WORKER" --format '{{.State.Status}}')" == "running" ]] || fail "stitch worker not running"
expected_image_id="$(docker image inspect "$NEW_IMAGE" --format '{{.Id}}')"
[[ "$(docker inspect "$WORKER" --format '{{.Image}}')" == "$expected_image_id" ]] || fail "stitch worker image mismatch"
docker exec "$WORKER" sh -lc \
  'grep -q "v3_scene_artifact_refresh import v3_scene_coordinator_loop" /app/app/workers/stitch_worker.py && grep -q "svc-fusion-full-status-artifact" /app/app/workers/v3_scene_artifact_refresh.py' \
  || fail "runtime artifact refresh source missing"
[[ "$(docker exec "$WORKER" printenv DF_V3_SCENE_COORDINATOR_ENABLED)" == "true" ]] || fail "scene coordinator disabled"
log "STITCH_WORKER_ONLY_CUTOVER=PASS"
log "STITCH_ARTIFACT_REFRESH_RUNTIME=PASS"

sleep 3
if docker logs --tail 120 "$WORKER" 2>&1 | grep -q 'V3 scene coordinator started'; then
  log "SCENE_COORDINATOR_STARTED=PASS"
else
  fail "new worker did not log V3 scene coordinator startup"
fi

log ""
log "===== 6. POST-CUTOVER INVARIANTS ====="
after_ids="$(child_ids)"
[[ "$after_ids" == "$before_ids" ]] || fail "child job ids changed during worker deployment"
log "ZERO_NEW_FUSION_CHILD_JOBS=PASS"
log "PRESERVED_CHILD_IDS_UNCHANGED=PASS"
after_non_targets="$(snapshot_non_targets)"
[[ "$after_non_targets" == "$before_non_targets" ]] || {
  diff -u <(printf '%s\n' "$before_non_targets") <(printf '%s\n' "$after_non_targets") || true
  fail "non-target runtime changed"
}
log "NON_TARGET_RUNTIME_UNCHANGED=PASS"

# The fix itself is now proven and should remain deployed. A user-authorized
# retry-stitch call is intentionally not forged here because it requires the real
# authenticated Director request. The existing UI supplies that authorization.
CUTOVER=0

log ""
log "============================================================"
log " DEV STITCH ARTIFACT URL REFRESH DEPLOYMENT PASS"
log "============================================================"
log "PRESERVED_9_VIDEO_CHILDREN=PASS"
log "DIRECTOR_RETRY_STITCH_ROUTE=PASS"
log "DIRECTOR_PRESERVED_URL_REFRESH_RUNTIME=PASS"
log "STITCH_ARTIFACT_REFRESH_IMAGE=PASS"
log "STITCH_WORKER_ONLY_CUTOVER=PASS"
log "STITCH_ARTIFACT_REFRESH_RUNTIME=PASS"
log "SCENE_COORDINATOR_STARTED=PASS"
log "ZERO_NEW_FUSION_CHILD_JOBS=PASS"
log "PRESERVED_CHILD_IDS_UNCHANGED=PASS"
log "NON_TARGET_RUNTIME_UNCHANGED=PASS"
log "PRODUCTION_TOUCH=NONE"
log "NEXT=HARD_REFRESH_EXISTING_STORY_THEN_CLICK_RESUME_SCENE_ASSEMBLY_ONCE"
