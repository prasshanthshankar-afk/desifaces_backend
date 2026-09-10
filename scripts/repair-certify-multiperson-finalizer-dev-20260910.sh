#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
REPO="/home/azureuser/workspace/desifaces-v3"
SOURCE_REF="dbbbacc705ef071ae46cdd40418e113c50d0abb3"
WORKFLOW_ID="16099052-15b5-401f-a447-c5d989b7b8ad"
STAGE_ID="cbf4b76a-21ec-4b17-951a-e0674a6f247f"
EXPECTED_CHILDREN=9
DB="desifaces-v3-db"
REDIS="desifaces-v3-redis"
WORKER="df-v3-svc-fusion-extension-stitch-worker"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WT="/tmp/desifaces-v3-finalizer-${STAMP}"
ENVFILE="/tmp/desifaces-v3-finalizer-env-${STAMP}"
NEW_IMAGE="desifaces-v3-finalizer:${SOURCE_REF:0:12}"
ROLLBACK_TAG="desifaces-v3-finalizer-rollback:${STAMP}"
CUTOVER=0
FINALIZER_PROVED=0
OLD_CONFIG_IMAGE=""
NON_TARGETS=(
  "$DB" "$REDIS"
  df-v3-svc-director df-v3-svc-director-worker
  df-v3-svc-fusion df-v3-svc-fusion-worker
  df-v3-svc-face df-v3-svc-face-worker
  df-v3-svc-audio df-v3-svc-audio-worker
  df-v3-svc-fusion-extension df-v3-web
)

log() { printf '%s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }

host="$(hostname -s)"
[[ "$host" == "$EXPECTED_HOST" ]] || fail "DEV host guard: expected=$EXPECTED_HOST current=$host"
[[ -d "$REPO/.git" ]] || fail "backend repo not found at $REPO"
command -v docker >/dev/null || fail "docker unavailable"
command -v git >/dev/null || fail "git unavailable"
docker inspect "$WORKER" >/dev/null 2>&1 || fail "$WORKER is missing"

compose() {
  (cd "$REPO" && docker compose -f docker-compose.yml -f docker-compose.v3.yml --profile v3-execution "$@")
}

psqlq() {
  local sql="$1"
  docker exec -e DF_FINALIZER_SQL="$sql" "$DB" sh -lc \
    'psql -X -A -t -F "|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_FINALIZER_SQL"'
}

snapshot_non_targets() {
  local c
  for c in "${NON_TARGETS[@]}"; do
    if docker inspect "$c" >/dev/null 2>&1; then
      docker inspect "$c" --format '{{.Name}}|{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}'
    else
      printf '/%s|MISSING|MISSING|MISSING\n' "$c"
    fi
  done | sort
}

child_ids() {
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

rollback_worker() {
  [[ "$CUTOVER" -eq 1 ]] || return 0
  [[ "$FINALIZER_PROVED" -eq 0 ]] || return 0
  log ""
  log "===== AUTOMATIC STITCH-WORKER ROLLBACK ====="
  if [[ -n "$OLD_CONFIG_IMAGE" ]] && docker image inspect "$ROLLBACK_TAG" >/dev/null 2>&1; then
    docker tag "$ROLLBACK_TAG" "$OLD_CONFIG_IMAGE" >/dev/null
    compose up -d --no-deps --no-build --force-recreate svc-fusion-extension-stitch-worker || true
    log "STITCH_WORKER_ROLLBACK=ATTEMPTED"
  else
    log "STITCH_WORKER_ROLLBACK=UNAVAILABLE"
  fi
}

cleanup() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then rollback_worker; fi
  rm -f "$ENVFILE" >/dev/null 2>&1 || true
  if [[ -d "$WT" ]]; then
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT" || true
  fi
  exit "$rc"
}
trap cleanup EXIT

log "============================================================"
log " desifaces DEV — V3 SCENE FINALIZER RUNTIME REPAIR"
log "============================================================"
log "host=$host"
log "environment=DEV_ONLY"
log "source_ref=$SOURCE_REF"
log "workflow_id=$WORKFLOW_ID"
log "stage_run_id=$STAGE_ID"
log "child_rerender=FORBIDDEN"
log "backend_api_restart=NONE"
log "fusion_restart=NONE"
log "audio_face_restart=NONE"
log "database_redis_restart=NONE"
log "production_touch=NONE"

log ""
log "===== 1. IMMUTABLE SOURCE + CONTRACT ====="
git -C "$REPO" fetch --quiet origin "$SOURCE_REF"
git -C "$REPO" worktree add --detach "$WT" "$SOURCE_REF" >/dev/null
[[ "$(git -C "$WT" rev-parse HEAD)" == "$SOURCE_REF" ]] || fail "immutable worktree SHA mismatch"
python3 -m py_compile \
  "$WT/services/svc-fusion-extension/app/app/workers/stitch_worker.py" \
  "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py"
python3 - <<PY
from pathlib import Path
root=Path("$WT")
w=(root/'services/svc-fusion-extension/app/app/workers/stitch_worker.py').read_text()
c=(root/'services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py').read_text()
v=(root/'docker-compose.v3.yml').read_text()
assert 'from app.workers.v3_scene_coordinator import v3_scene_coordinator_loop' in w
assert 'v3_scene_coordinator_loop()' in w
assert "a.state in ('running','succeeded')" in c
assert "{fusion_parent_pricing,state}" in c
assert "('reserved','commit_pending')" in c
assert 'phase="scene_stitch"' in c
assert 'commit_scene_pricing' in c
svc=v.split('svc-fusion-extension-stitch-worker:',1)[1].split('\n  svc-music-worker:',1)[0]
assert 'DF_V3_SCENE_COORDINATOR_ENABLED: "true"' in svc
print('SCENE_COORDINATOR_SOURCE_CONTRACT=PASS')
PY

log ""
log "===== 2. LIVE SCENE PREFLIGHT — NO MUTATION ====="
preflight="$(psqlq "
select s.state::text,
       coalesce(s.metadata_json #>> '{aspect_ratio}',''),
       coalesce(s.metadata_json #>> '{fusion_parent_pricing,state}',''),
       a.state::text,
       coalesce(jsonb_array_length(a.metadata_json->'children'),0)::text,
       coalesce(a.metadata_json #>> '{background_coordinator,phase}',''),
       (select count(*) from public.v3_studio_stage_outputs o where o.stage_run_id=s.stage_run_id and o.is_active=true)::text
from public.v3_studio_stage_runs s
join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
join lateral (
  select state,metadata_json from public.v3_studio_stage_attempts
  where stage_run_id=s.stage_run_id order by attempt_no desc limit 1
) a on true
where s.stage_run_id='${STAGE_ID}'::uuid and s.workflow_id='${WORKFLOW_ID}'::uuid;")"
[[ -n "$preflight" ]] || fail "target Scene not found"
IFS='|' read -r scene_state aspect parent_state attempt_state attempt_children coordinator_phase output_count <<<"$preflight"
log "scene_state=$scene_state"
log "aspect_ratio=${aspect:-unknown}"
log "parent_pricing_state=${parent_state:-NONE}"
log "attempt_state=$attempt_state"
log "attempt_recorded_children=$attempt_children"
log "coordinator_phase=${coordinator_phase:-NONE}"
log "active_scene_outputs=$output_count"

if [[ "$scene_state" == "awaiting_review" || "$scene_state" == "approved" ]]; then
  [[ "$output_count" -ge 1 ]] || fail "terminal Scene has no active output"
  log "SCENE_ALREADY_FINALIZED=PASS"
  FINALIZER_PROVED=1
  log "PRODUCTION_TOUCH=NONE"
  exit 0
fi
[[ "$scene_state" == "generating" ]] || fail "expected generating Scene, got $scene_state"
[[ "$aspect" == "16:9" ]] || fail "expected preserved 16:9 Scene, got ${aspect:-missing}"
[[ "$attempt_state" == "running" || "$attempt_state" == "succeeded" ]] || fail "attempt not finalizer-eligible: $attempt_state"
[[ "$parent_state" == "reserved" || "$parent_state" == "commit_pending" ]] || fail "parent pricing not finalizer-eligible: ${parent_state:-missing}"
[[ "$attempt_children" -eq "$EXPECTED_CHILDREN" ]] || fail "attempt child lineage expected $EXPECTED_CHILDREN, got $attempt_children"

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
[[ "$candidate_count" == "1" ]] || fail "current source candidate predicate does not select target Scene"
log "FINALIZER_CANDIDATE_SQL=PASS"

before_child_ids="$(child_ids)"
before_child_count="$(printf '%s\n' "$before_child_ids" | sed '/^$/d' | wc -l | tr -d ' ')"
[[ "$before_child_count" -eq "$EXPECTED_CHILDREN" ]] || fail "expected $EXPECTED_CHILDREN durable Fusion children, got $before_child_count"
child_id_csv="$(printf '%s\n' "$before_child_ids" | sed '/^$/d' | paste -sd, -)"
child_summary="$(psqlq "
select count(*)::text,
       count(*) filter(where j.status='succeeded')::text,
       count(*) filter(where j.status in ('queued','processing','running','submitted','pending'))::text,
       count(*) filter(where j.status in ('failed','cancelled','canceled','blocked'))::text
from public.studio_jobs j
where j.id=any(string_to_array('${child_id_csv}',',')::uuid[]);")"
IFS='|' read -r total_children succeeded_children active_children failed_children <<<"$child_summary"
log "fusion_video_children=$total_children"
log "fusion_video_children_succeeded=$succeeded_children"
log "fusion_video_children_active=$active_children"
log "fusion_video_children_failed=$failed_children"
[[ "$succeeded_children" -eq "$EXPECTED_CHILDREN" && "$active_children" -eq 0 && "$failed_children" -eq 0 ]] || fail "existing child videos are not all safely complete"
log "PRESERVED_9_VIDEO_CHILDREN=PASS"

before_non_targets="$(snapshot_non_targets)"

log ""
log "===== 3. PROVE RUNTIME DRIFT / WIRING ====="
source_coord_sha="$(sha256sum "$WT/services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py" | cut -d' ' -f1)"
runtime_coord_sha="$(docker exec "$WORKER" sh -lc 'sha256sum /app/app/workers/v3_scene_coordinator.py 2>/dev/null | cut -d" " -f1' 2>/dev/null || true)"
runtime_worker_has_coordinator="$(docker exec "$WORKER" sh -lc 'grep -c "v3_scene_coordinator_loop" /app/app/workers/stitch_worker.py 2>/dev/null || true' 2>/dev/null || true)"
runtime_cmd="$(docker inspect "$WORKER" --format '{{json .Config.Cmd}}')"
log "worker_command=$runtime_cmd"
if [[ "$runtime_coord_sha" == "$source_coord_sha" && "${runtime_worker_has_coordinator:-0}" -gt 0 ]]; then
  log "STITCH_WORKER_COORDINATOR_SOURCE=CURRENT"
else
  log "STITCH_WORKER_COORDINATOR_SOURCE=STALE_OR_MISSING"
fi
if docker logs --tail 400 "$WORKER" 2>&1 | grep -q 'V3 scene coordinator iteration failed'; then
  log "STITCH_WORKER_COORDINATOR_ERROR_LOOP=DETECTED"
else
  log "STITCH_WORKER_COORDINATOR_ERROR_LOOP=NOT_SEEN_IN_TAIL"
fi

log ""
log "===== 4. BUILD IMMUTABLE FINALIZER IMAGE ====="
docker build --pull=false \
  -t "$NEW_IMAGE" \
  -f "$WT/services/svc-fusion-extension/app/Dockerfile" \
  "$WT"

docker run --rm --entrypoint sh "$NEW_IMAGE" -lc \
  'test -s /app/app/workers/v3_scene_coordinator.py && test -s /app/app/workers/stitch_worker.py && grep -q "v3_scene_coordinator_loop" /app/app/workers/stitch_worker.py'
log "FINALIZER_IMAGE_WIRING=PASS"

# Reuse the running worker's environment only inside a chmod-600 transient file.
# Nothing is printed; the file is deleted by the EXIT trap.
umask 077
docker inspect "$WORKER" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$ENVFILE"
chmod 600 "$ENVFILE"

log ""
log "===== 5. ISOLATED CANDIDATE CLAIM PROOF ====="
docker run --rm -i \
  --network df-v3-net \
  --env-file "$ENVFILE" \
  --entrypoint python "$NEW_IMAGE" - <<PY
import asyncio
from app.db import get_db_pool
from app.workers.v3_scene_coordinator import _candidate_rows

async def main():
    pool = await get_db_pool()
    rows = await _candidate_rows(pool)
    ids = {str(row['stage_run_id']) for row in rows}
    assert '$STAGE_ID' in ids, f'target scene not selected; candidate_count={len(rows)} ids={sorted(ids)}'
    print('FINALIZER_IMAGE_LIVE_CANDIDATE=PASS')
    print(f'candidate_count={len(rows)}')
    await pool.close()

asyncio.run(main())
PY

log ""
log "===== 6. STITCH-WORKER-ONLY CUTOVER ====="
old_image_id="$(docker inspect "$WORKER" --format '{{.Image}}')"
OLD_CONFIG_IMAGE="$(docker inspect "$WORKER" --format '{{.Config.Image}}')"
[[ -n "$old_image_id" && -n "$OLD_CONFIG_IMAGE" ]] || fail "could not capture worker rollback image"
[[ "$OLD_CONFIG_IMAGE" != sha256:* ]] || fail "worker configured image is not tag-addressable: $OLD_CONFIG_IMAGE"
docker tag "$old_image_id" "$ROLLBACK_TAG"
docker tag "$NEW_IMAGE" "$OLD_CONFIG_IMAGE"
compose up -d --no-deps --no-build --force-recreate svc-fusion-extension-stitch-worker
CUTOVER=1

for i in $(seq 1 40); do
  state="$(docker inspect "$WORKER" --format '{{.State.Status}}' 2>/dev/null || true)"
  if [[ "$state" == "running" ]]; then break; fi
  sleep 1
done
[[ "$(docker inspect "$WORKER" --format '{{.State.Status}}')" == "running" ]] || fail "new stitch worker did not stay running"
new_runtime_image="$(docker inspect "$WORKER" --format '{{.Image}}')"
new_expected_id="$(docker image inspect "$NEW_IMAGE" --format '{{.Id}}')"
[[ "$new_runtime_image" == "$new_expected_id" ]] || fail "stitch worker did not cut over to immutable candidate image"
[[ "$(docker exec "$WORKER" printenv DF_V3_SCENE_COORDINATOR_ENABLED)" == "true" ]] || fail "coordinator not enabled after cutover"
log "STITCH_WORKER_ONLY_CUTOVER=PASS"

log ""
log "===== 7. SERVER-SIDE FAN-IN / STITCH / PRICING COMMIT ====="
last=""
for i in $(seq 1 180); do
  status="$(psqlq "
select s.state::text,
       coalesce(s.metadata_json #>> '{fusion_parent_pricing,state}',''),
       coalesce(a.metadata_json #>> '{background_coordinator,phase}',''),
       coalesce(a.error_code::text,''),
       coalesce(a.error_message::text,''),
       (select count(*) from public.v3_studio_stage_outputs o where o.stage_run_id=s.stage_run_id and o.is_active=true)::text,
       (select count(*) from public.v3_studio_review_items r join public.v3_studio_stage_outputs o on o.stage_run_id=r.stage_run_id and o.media_id=r.media_id where r.stage_run_id=s.stage_run_id and o.is_active=true and r.decision='pending')::text
from public.v3_studio_stage_runs s
join lateral (
 select state,metadata_json,error_code,error_message from public.v3_studio_stage_attempts
 where stage_run_id=s.stage_run_id order by attempt_no desc limit 1
) a on true
where s.stage_run_id='${STAGE_ID}'::uuid;")"
  IFS='|' read -r st pp phase errcode errmsg outputs pending_review <<<"$status"
  compact="state=$st parent=${pp:-NONE} phase=${phase:-NONE} outputs=$outputs pending_review=$pending_review"
  if [[ "$compact" != "$last" ]]; then log "$compact"; last="$compact"; fi

  if [[ "$st" == "failed" ]]; then
    log "scene_error_code=${errcode:-NONE}"
    log "scene_error_message=${errmsg:-NONE}"
    fail "server-side finalization moved Scene to failed"
  fi
  if [[ "$st" == "awaiting_review" && "$pp" == "committed" && "$outputs" -ge 1 && "$pending_review" -ge 1 ]]; then
    FINALIZER_PROVED=1
    log "SERVER_SIDE_SCENE_FINALIZATION=PASS"
    break
  fi
  if [[ "$st" == "approved" && "$pp" == "committed" && "$outputs" -ge 1 ]]; then
    FINALIZER_PROVED=1
    log "SERVER_SIDE_SCENE_FINALIZATION=PASS_ALREADY_APPROVED"
    break
  fi
  sleep 5
done
[[ "$FINALIZER_PROVED" -eq 1 ]] || fail "timed out waiting for server-side Scene finalization"

log ""
log "===== 8. ZERO-RERENDER + NON-TARGET INVARIANTS ====="
after_child_ids="$(child_ids)"
[[ "$after_child_ids" == "$before_child_ids" ]] || fail "Fusion child lineage changed; new child generation is forbidden in this repair"
log "ZERO_NEW_FUSION_CHILD_JOBS=PASS"
log "PRESERVED_CHILD_IDS_UNCHANGED=PASS"

after_non_targets="$(snapshot_non_targets)"
if [[ "$after_non_targets" != "$before_non_targets" ]]; then
  log "NON_TARGET_RUNTIME_UNCHANGED=FAIL"
  diff -u <(printf '%s\n' "$before_non_targets") <(printf '%s\n' "$after_non_targets") || true
  fail "one or more non-target runtimes changed during finalizer repair"
fi
log "NON_TARGET_RUNTIME_UNCHANGED=PASS"
log "DIRECTOR_FUSION_FACE_AUDIO_UNCHANGED=PASS"
log "DB_REDIS_UNCHANGED=PASS"

log ""
log "============================================================"
log " DEV MULTI-PERSON SERVER-SIDE FINALIZER REPAIR PASS"
log "============================================================"
log "SCENE_COORDINATOR_SOURCE_CONTRACT=PASS"
log "FINALIZER_CANDIDATE_SQL=PASS"
log "PRESERVED_9_VIDEO_CHILDREN=PASS"
log "FINALIZER_IMAGE_WIRING=PASS"
log "FINALIZER_IMAGE_LIVE_CANDIDATE=PASS"
log "STITCH_WORKER_ONLY_CUTOVER=PASS"
log "SERVER_SIDE_SCENE_FINALIZATION=PASS"
log "ZERO_NEW_FUSION_CHILD_JOBS=PASS"
log "PRESERVED_CHILD_IDS_UNCHANGED=PASS"
log "NON_TARGET_RUNTIME_UNCHANGED=PASS"
log "PRODUCTION_TOUCH=NONE"
log "NEXT=HARD_REFRESH_EXISTING_STORY_AND_REVIEW_FINAL_16_9_SCENE"
