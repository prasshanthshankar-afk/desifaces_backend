#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REPO="/home/azureuser/workspace/desifaces-v3"
SOURCE_REF="79dcdd359291b3cfc60674c47be0636607b0a53c"
WORKFLOW_ID="16099052-15b5-401f-a447-c5d989b7b8ad"
STAGE_ID="cbf4b76a-21ec-4b17-951a-e0674a6f247f"
EXPECTED_CHILDREN=9
DB="desifaces-v3-db"
WORKER="df-v3-svc-fusion-extension-stitch-worker"
ENVFILE="$REPO/infra/.env"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WT="/tmp/desifaces-v3-finalizer-resume-${STAMP}"
RUN_ENV="/tmp/desifaces-v3-finalizer-runtime-env-${STAMP}"
NEW_IMAGE="desifaces-v3-finalizer:${SOURCE_REF:0:12}"
ROLLBACK_TAG="desifaces-v3-finalizer-rollback:${STAMP}"
TARGET_STARTED=0
FINALIZED=0

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
[[ -s "$ENVFILE" ]] || fail "required compose interpolation env missing: $ENVFILE"
command -v docker >/dev/null || fail "docker unavailable"
command -v git >/dev/null || fail "git unavailable"
docker inspect "$WORKER" >/dev/null 2>&1 || fail "$WORKER missing"

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

cleanup(){
  local rc=$?
  rm -f "$RUN_ENV" >/dev/null 2>&1 || true
  if [[ -d "$WT" ]]; then
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT" || true
  fi
  exit "$rc"
}
trap cleanup EXIT

log "============================================================"
log " desifaces DEV — RESTART-SAFE FINALIZER REPAIR RESUME"
log "============================================================"
log "environment=DEV_ONLY"
log "production_touch=NONE"
log "provider_generation=NONE"
log "child_rerender=FORBIDDEN"
log "target_stage=$STAGE_ID"

log ""
log "===== 1. RECONCILE IMAGE TAG LEFT BY FAILED HARNESS ====="
RUNNING_IMAGE_ID="$(docker inspect "$WORKER" --format '{{.Image}}')"
CONFIG_IMAGE="$(docker inspect "$WORKER" --format '{{.Config.Image}}')"
[[ -n "$RUNNING_IMAGE_ID" && -n "$CONFIG_IMAGE" ]] || fail "cannot resolve running worker image"
[[ "$CONFIG_IMAGE" != sha256:* ]] || fail "configured worker image is not tag-addressable: $CONFIG_IMAGE"
docker tag "$RUNNING_IMAGE_ID" "$CONFIG_IMAGE"
log "RUNNING_WORKER_TAG_RESTORED=PASS"

log ""
log "===== 2. COMPOSE INTERPOLATION PREFLIGHT — BEFORE CUTOVER ====="
compose config --services >/dev/null
log "COMPOSE_ENV_INTERPOLATION=PASS"

log ""
log "===== 3. IMMUTABLE SOURCE + LIVE SCENE SAFETY ====="
git -C "$REPO" fetch --quiet origin "$SOURCE_REF"
git -C "$REPO" worktree add --detach "$WT" "$SOURCE_REF" >/dev/null
[[ "$(git -C "$WT" rev-parse HEAD)" == "$SOURCE_REF" ]] || fail "immutable source mismatch"
python3 -m py_compile "$WT/services/svc-fusion-extension/app/app/workers/stitch_worker.py" "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py"

scene="$(psqlq "
select s.state::text,
       coalesce(s.metadata_json #>> '{aspect_ratio}',''),
       coalesce(s.metadata_json #>> '{fusion_parent_pricing,state}',''),
       a.state::text,
       coalesce(jsonb_array_length(a.metadata_json->'children'),0)::text,
       coalesce(a.metadata_json #>> '{background_coordinator,phase}',''),
       (select count(*) from public.v3_studio_stage_outputs o where o.stage_run_id=s.stage_run_id and o.is_active=true)::text
from public.v3_studio_stage_runs s
join lateral (
  select state,metadata_json from public.v3_studio_stage_attempts
  where stage_run_id=s.stage_run_id order by attempt_no desc limit 1
) a on true
where s.stage_run_id='${STAGE_ID}'::uuid and s.workflow_id='${WORKFLOW_ID}'::uuid;")"
[[ -n "$scene" ]] || fail "target Scene missing"
IFS='|' read -r scene_state aspect parent_state attempt_state attempt_children phase output_count <<<"$scene"
log "scene_state=$scene_state"
log "aspect_ratio=${aspect:-NONE}"
log "parent_pricing_state=${parent_state:-NONE}"
log "attempt_state=$attempt_state"
log "attempt_recorded_children=$attempt_children"
log "coordinator_phase=${phase:-NONE}"

if [[ "$scene_state" == "awaiting_review" || "$scene_state" == "approved" ]]; then
  [[ "$output_count" -ge 1 ]] || fail "terminal Scene has no active output"
  log "SCENE_ALREADY_FINALIZED=PASS"
  FINALIZED=1
  exit 0
fi
[[ "$scene_state" == "generating" ]] || fail "Scene is not generating: $scene_state"
[[ "$aspect" == "16:9" ]] || fail "Scene aspect changed: ${aspect:-missing}"
[[ "$parent_state" == "reserved" || "$parent_state" == "commit_pending" ]] || fail "Scene parent pricing is not finalizer-eligible: ${parent_state:-missing}"
[[ "$attempt_state" == "running" || "$attempt_state" == "succeeded" ]] || fail "Scene attempt is not finalizer-eligible: $attempt_state"
[[ "$attempt_children" -eq "$EXPECTED_CHILDREN" ]] || fail "Director attempt expected $EXPECTED_CHILDREN children, got $attempt_children"

BEFORE_IDS="$(child_ids)"
BEFORE_COUNT="$(printf '%s\n' "$BEFORE_IDS" | sed '/^$/d' | wc -l | tr -d ' ')"
[[ "$BEFORE_COUNT" -eq "$EXPECTED_CHILDREN" ]] || fail "expected $EXPECTED_CHILDREN Fusion child jobs, got $BEFORE_COUNT"
IDCSV="$(printf '%s\n' "$BEFORE_IDS" | paste -sd, -)"
summary="$(psqlq "select count(*)::text,count(*) filter(where status='succeeded')::text,count(*) filter(where status in ('queued','processing','running','submitted','pending'))::text,count(*) filter(where status in ('failed','cancelled','canceled','blocked'))::text from public.studio_jobs where id=any(string_to_array('${IDCSV}',',')::uuid[]);")"
IFS='|' read -r total succeeded active failed <<<"$summary"
log "video_children_total=$total"
log "video_children_succeeded=$succeeded"
log "video_children_active=$active"
log "video_children_failed=$failed"
[[ "$succeeded" -eq "$EXPECTED_CHILDREN" && "$active" -eq 0 && "$failed" -eq 0 ]] || fail "nine preserved video children are not all succeeded"
log "PRESERVED_9_VIDEO_CHILDREN=PASS"

candidate_count="$(psqlq "
select count(*)::text
from public.v3_studio_stage_runs s
join lateral (
  select state,metadata_json from public.v3_studio_stage_attempts
  where stage_run_id=s.stage_run_id order by attempt_no desc limit 1
) a on true
where s.stage_run_id='${STAGE_ID}'::uuid
  and s.stage_type='fusion' and s.scope_type='scene' and s.state='generating'
  and a.state in ('running','succeeded')
  and coalesce(s.metadata_json #>> '{fusion_parent_pricing,state}','') in ('reserved','commit_pending');")"
[[ "$candidate_count" == "1" ]] || fail "target Scene does not satisfy current coordinator candidate predicate"
log "FINALIZER_CANDIDATE_SQL=PASS"

BEFORE_NON_TARGETS="$(snapshot_non_targets)"

log ""
log "===== 4. BUILD/VERIFY CURRENT FINALIZER IMAGE ====="
docker build --pull=false -t "$NEW_IMAGE" -f "$WT/services/svc-fusion-extension/app/Dockerfile" "$WT"
docker run --rm --entrypoint sh "$NEW_IMAGE" -lc 'test -s /app/app/workers/v3_scene_coordinator.py && grep -q "v3_scene_coordinator_loop" /app/app/workers/stitch_worker.py'
log "FINALIZER_IMAGE_WIRING=PASS"

umask 077
docker inspect "$WORKER" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$RUN_ENV"
chmod 600 "$RUN_ENV"
docker run --rm -i --network df-v3-net --env-file "$RUN_ENV" --entrypoint python "$NEW_IMAGE" - <<PY
import asyncio
from app.db import get_db_pool
from app.workers.v3_scene_coordinator import _candidate_rows
async def main():
    pool = await get_db_pool()
    rows = await _candidate_rows(pool)
    ids = {str(r['stage_run_id']) for r in rows}
    assert '$STAGE_ID' in ids, f'target not selected; candidates={len(rows)}'
    print('FINALIZER_IMAGE_LIVE_CANDIDATE=PASS')
    await pool.close()
asyncio.run(main())
PY

log ""
log "===== 5. STITCH-WORKER-ONLY CUTOVER ====="
OLD_IMAGE_ID="$RUNNING_IMAGE_ID"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_TAG"
docker tag "$NEW_IMAGE" "$CONFIG_IMAGE"
if ! compose up -d --no-deps --no-build --force-recreate svc-fusion-extension-stitch-worker; then
  docker tag "$ROLLBACK_TAG" "$CONFIG_IMAGE" >/dev/null 2>&1 || true
  compose up -d --no-deps --no-build --force-recreate svc-fusion-extension-stitch-worker >/dev/null 2>&1 || true
  fail "stitch-worker cutover failed; rollback attempted"
fi
TARGET_STARTED=1
for i in $(seq 1 40); do
  [[ "$(docker inspect "$WORKER" --format '{{.State.Status}}' 2>/dev/null || true)" == "running" ]] && break
  sleep 1
done
[[ "$(docker inspect "$WORKER" --format '{{.State.Status}}')" == "running" ]] || fail "new stitch worker did not remain running"
[[ "$(docker exec "$WORKER" printenv DF_V3_SCENE_COORDINATOR_ENABLED)" == "true" ]] || fail "coordinator disabled after cutover"
NEW_RUNTIME_SHA="$(docker exec "$WORKER" sha256sum /app/app/workers/v3_scene_coordinator.py | cut -d' ' -f1)"
SOURCE_SHA="$(sha256sum "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py" | cut -d' ' -f1)"
[[ "$NEW_RUNTIME_SHA" == "$SOURCE_SHA" ]] || fail "new stitch-worker runtime source does not match immutable source"
log "STITCH_WORKER_ONLY_CUTOVER=PASS"
log "STITCH_WORKER_RUNTIME_SOURCE=CURRENT"

log ""
log "===== 6. SERVER-SIDE FAN-IN -> STITCH -> PRICING COMMIT -> REVIEW ====="
last=""
for i in $(seq 1 180); do
  snap="$(psqlq "
select s.state::text,
       coalesce(s.metadata_json #>> '{fusion_parent_pricing,state}',''),
       a.state::text,
       coalesce(a.metadata_json #>> '{background_coordinator,phase}',''),
       (select count(*) from public.v3_studio_stage_outputs o where o.stage_run_id=s.stage_run_id and o.is_active=true)::text,
       coalesce(a.error_code::text,''),coalesce(a.error_message::text,'')
from public.v3_studio_stage_runs s
join lateral (
  select state,metadata_json,error_code,error_message from public.v3_studio_stage_attempts
  where stage_run_id=s.stage_run_id order by attempt_no desc limit 1
) a on true
where s.stage_run_id='${STAGE_ID}'::uuid;")"
  if [[ "$snap" != "$last" ]]; then log "progress=$snap"; last="$snap"; fi
  IFS='|' read -r st pp ast ph out ec em <<<"$snap"
  if [[ "$st" == "awaiting_review" || "$st" == "approved" ]]; then
    [[ "$pp" == "committed" ]] || fail "Scene reached review without committed parent pricing: $pp"
    [[ "$out" -ge 1 ]] || fail "Scene reached review without active output"
    FINALIZED=1
    break
  fi
  if [[ "$st" == "failed" || "$ast" == "failed" ]]; then
    docker logs --tail 120 "$WORKER" 2>&1 | grep -E 'v3_scene|scene coordinator|stitch|ERROR|Traceback' | tail -n 60 || true
    fail "Scene finalization failed: code=${ec:-none} message=${em:-none}"
  fi
  sleep 2
done
[[ "$FINALIZED" -eq 1 ]] || fail "Scene did not reach review within certification window"
log "SERVER_SIDE_SCENE_FINALIZATION=PASS"

log ""
log "===== 7. ZERO-RERENDER + NON-TARGET INVARIANTS ====="
AFTER_IDS="$(child_ids)"
[[ "$AFTER_IDS" == "$BEFORE_IDS" ]] || fail "Fusion child job IDs changed during finalization"
log "ZERO_NEW_FUSION_CHILD_JOBS=PASS"
log "PRESERVED_CHILD_IDS_UNCHANGED=PASS"
AFTER_NON_TARGETS="$(snapshot_non_targets)"
[[ "$AFTER_NON_TARGETS" == "$BEFORE_NON_TARGETS" ]] || {
  diff -u <(printf '%s\n' "$BEFORE_NON_TARGETS") <(printf '%s\n' "$AFTER_NON_TARGETS") || true
  fail "non-target runtime changed"
}
log "NON_TARGET_RUNTIME_UNCHANGED=PASS"

log ""
log "============================================================"
log " DEV MULTI-PERSON SERVER-SIDE FINALIZER REPAIR PASS"
log "============================================================"
log "PRESERVED_9_VIDEO_CHILDREN=PASS"
log "FINALIZER_CANDIDATE_SQL=PASS"
log "FINALIZER_IMAGE_WIRING=PASS"
log "FINALIZER_IMAGE_LIVE_CANDIDATE=PASS"
log "STITCH_WORKER_ONLY_CUTOVER=PASS"
log "SERVER_SIDE_SCENE_FINALIZATION=PASS"
log "ZERO_NEW_FUSION_CHILD_JOBS=PASS"
log "PRESERVED_CHILD_IDS_UNCHANGED=PASS"
log "NON_TARGET_RUNTIME_UNCHANGED=PASS"
log "PRODUCTION_TOUCH=NONE"
log "NEXT=HARD_REFRESH_EXISTING_STORY_AND_REVIEW_FINAL_16_9_SCENE"
