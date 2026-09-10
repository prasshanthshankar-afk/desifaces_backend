#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
ROOT="/home/azureuser/workspace/desifaces-v3"
WORKER="df-v3-svc-director-worker"
API="df-v3-svc-director"
DB="desifaces-v3-db"
REDIS="desifaces-v3-redis"
FACE="df-v3-svc-face"
AUDIO="df-v3-svc-audio"
FUSION="df-v3-svc-fusion"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK_IMAGE="desifaces-dev-director-worker-rollback:${STAMP}"
WORKER_REPLACED=0
ROLLBACK_READY=0
SUCCESS=0

fail(){ echo "FAIL: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }
for x in docker curl python3; do need "$x"; done
[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "run only on $EXPECTED_HOST; current=$(hostname -s)"
[[ -d "$ROOT/.git" ]] || fail "dev checkout missing: $ROOT"
[[ -f "$ROOT/infra/.env" ]] || fail "dev env missing: $ROOT/infra/.env"
[[ -f "$ROOT/scripts/v3-compose.sh" ]] || fail "v3 compose launcher missing"

for c in "$API" "$DB" "$REDIS" "$FACE" "$AUDIO" "$FUSION"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "required dev container missing: $c"
done

snapshot(){ docker inspect "$1" --format '{{.Id}}|{{.State.StartedAt}}|{{.Image}}|{{.Config.Image}}'; }
API_BEFORE="$(snapshot "$API")"
DB_BEFORE="$(snapshot "$DB")"
REDIS_BEFORE="$(snapshot "$REDIS")"
FACE_BEFORE="$(snapshot "$FACE")"
AUDIO_BEFORE="$(snapshot "$AUDIO")"
FUSION_BEFORE="$(snapshot "$FUSION")"

compose(){ bash "$ROOT/scripts/v3-compose.sh" --profile v3-orchestration "$@"; }

psqlq(){
  docker exec "$DB" sh -lc 'psql -Atq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' -- "$1"
}

latest_run(){
  psqlq "select coalesce(run_id::text,'')||'|'||coalesce(thread_id,'')||'|'||coalesce(state,'')||'|'||coalesce(attempt_count::text,'0')||'|'||coalesce(last_error,'') from public.v3_director_runs order by created_at desc limit 1;"
}

queued_count(){
  psqlq "select count(*) from public.v3_director_runs where state='queued' and available_at<=now() and attempt_count<max_attempts;"
}

run_state(){
  local run_id="$1"
  psqlq "select coalesce(state,'')||'|'||coalesce(attempt_count::text,'0')||'|'||coalesce(last_error,'') from public.v3_director_runs where run_id='${run_id}'::uuid;"
}

cleanup(){
  local rc=$?
  set +e
  if (( rc != 0 && SUCCESS == 0 && WORKER_REPLACED == 1 && ROLLBACK_READY == 1 )); then
    echo "===== DEV DIRECTOR WORKER AUTOMATIC ROLLBACK ====="
    docker rm -f "$WORKER" >/dev/null 2>&1 || true
    docker run -d \
      --name "$WORKER" \
      --restart unless-stopped \
      --network df-v3-net \
      "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
    echo "DIRECTOR_WORKER_ROLLBACK=ATTEMPTED"
  fi
  if (( SUCCESS == 1 && ROLLBACK_READY == 1 )); then
    docker image rm "$ROLLBACK_IMAGE" >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT

cat <<EOF
============================================================
 desifaces DEV — DIRECTOR QUEUE RECOVERY
============================================================
host=$(hostname -s)
environment=DEV_ONLY
production_touch=FORBIDDEN
scope=Director worker only
api_restart=FORBIDDEN
face_restart=FORBIDDEN
audio_restart=FORBIDDEN
fusion_restart=FORBIDDEN
db_restart=FORBIDDEN
redis_restart=FORBIDDEN
EOF

echo
echo "===== 1. VERIFY CURRENT QUEUE ====="
LATEST="$(latest_run)"
IFS='|' read -r RUN_ID THREAD_ID RUN_STATE ATTEMPT_COUNT LAST_ERROR <<<"$LATEST"
[[ -n "$RUN_ID" ]] || fail "no Director runs found"
QUEUE_COUNT="$(queued_count)"
echo "latest_run_id=$RUN_ID"
echo "latest_thread_id=$THREAD_ID"
echo "latest_state=$RUN_STATE"
echo "latest_attempt_count=$ATTEMPT_COUNT"
echo "eligible_queued_count=$QUEUE_COUNT"

if [[ "$RUN_STATE" != "queued" ]]; then
  echo "QUEUE_RECOVERY_NOT_REQUIRED=latest_run_state_${RUN_STATE}"
  SUCCESS=1
  exit 0
fi

if [[ "$QUEUE_COUNT" == "0" ]]; then
  fail "latest run is queued but not claimable; inspect available_at/max_attempts contract"
fi

echo
echo "===== 2. INSPECT DIRECTOR WORKER ====="
if docker inspect "$WORKER" >/dev/null 2>&1; then
  WORKER_STATUS="$(docker inspect "$WORKER" --format '{{.State.Status}}')"
  WORKER_IMAGE="$(docker inspect "$WORKER" --format '{{.Config.Image}}')"
  WORKER_IMAGE_ID="$(docker inspect "$WORKER" --format '{{.Image}}')"
  echo "worker_present=YES"
  echo "worker_status=$WORKER_STATUS"
  echo "worker_image=$WORKER_IMAGE"
  echo "worker_image_id=$WORKER_IMAGE_ID"
  echo "worker_recent_logs_begin"
  docker logs --tail 40 "$WORKER" 2>&1 | sed -E 's/(sk-[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-]+/\1...[REDACTED]/g' || true
  echo "worker_recent_logs_end"
else
  WORKER_STATUS="missing"
  echo "worker_present=NO"
fi

CURRENT_DIRECTOR_IMAGE="$(docker image inspect desifaces-v3-svc-director:latest --format '{{.Id}}' 2>/dev/null || true)"
[[ -n "$CURRENT_DIRECTOR_IMAGE" ]] || fail "certified dev Director image desifaces-v3-svc-director:latest missing"
echo "certified_director_image_id=$CURRENT_DIRECTOR_IMAGE"

# Give an already-running worker a short chance to claim the queued job before touching it.
if [[ "$WORKER_STATUS" == "running" ]]; then
  echo
echo "===== 3. PASSIVE CLAIM WINDOW ====="
  for i in $(seq 1 6); do
    STATE_ROW="$(run_state "$RUN_ID")"
    IFS='|' read -r STATE NOW_ATTEMPTS NOW_ERROR <<<"$STATE_ROW"
    echo "passive_check=$i state=$STATE attempts=$NOW_ATTEMPTS"
    if [[ "$STATE" != "queued" ]]; then
      echo "DIRECTOR_QUEUE_CLAIMED_WITHOUT_RESTART=PASS"
      SUCCESS=1
      break
    fi
    sleep 2
  done
fi

if (( SUCCESS == 0 )); then
  echo
echo "===== 4. REPLACE DIRECTOR WORKER ONLY ====="
  if docker inspect "$WORKER" >/dev/null 2>&1; then
    docker commit "$WORKER" "$ROLLBACK_IMAGE" >/dev/null
    ROLLBACK_READY=1
    echo "DIRECTOR_WORKER_ROLLBACK_IMAGE=PASS"
    docker rm -f "$WORKER" >/dev/null
  fi

  compose up -d --no-deps svc-director-worker
  WORKER_REPLACED=1

  for i in $(seq 1 30); do
    WORKER_STATUS="$(docker inspect "$WORKER" --format '{{.State.Status}}' 2>/dev/null || true)"
    echo "worker_wait=$i status=${WORKER_STATUS:-missing}"
    [[ "$WORKER_STATUS" == "running" ]] && break
    sleep 1
  done
  [[ "$WORKER_STATUS" == "running" ]] || {
    docker logs --tail 120 "$WORKER" 2>&1 || true
    fail "Director worker did not become running"
  }
  echo "DIRECTOR_WORKER_RUNNING=PASS"

  echo
echo "===== 5. VERIFY QUEUED RUN IS CLAIMED ====="
  for i in $(seq 1 30); do
    STATE_ROW="$(run_state "$RUN_ID")"
    IFS='|' read -r STATE NOW_ATTEMPTS NOW_ERROR <<<"$STATE_ROW"
    echo "claim_check=$i state=$STATE attempts=$NOW_ATTEMPTS"
    case "$STATE" in
      running|awaiting_review|ready)
        echo "DIRECTOR_QUEUE_CLAIM=PASS"
        SUCCESS=1
        break
        ;;
      failed)
        echo "director_last_error=${NOW_ERROR:0:800}"
        fail "Director worker claimed the run but execution failed"
        ;;
    esac
    sleep 1
  done
  (( SUCCESS == 1 )) || {
    docker logs --tail 160 "$WORKER" 2>&1 || true
    fail "queued Director run was not claimed within 30 seconds"
  }
fi

echo
echo "===== 6. NON-TARGET RUNTIME INVARIANTS ====="
[[ "$API_BEFORE" == "$(snapshot "$API")" ]] || fail "Director API changed or restarted"
[[ "$DB_BEFORE" == "$(snapshot "$DB")" ]] || fail "DB changed or restarted"
[[ "$REDIS_BEFORE" == "$(snapshot "$REDIS")" ]] || fail "Redis changed or restarted"
[[ "$FACE_BEFORE" == "$(snapshot "$FACE")" ]] || fail "Face API changed or restarted"
[[ "$AUDIO_BEFORE" == "$(snapshot "$AUDIO")" ]] || fail "Audio API changed or restarted"
[[ "$FUSION_BEFORE" == "$(snapshot "$FUSION")" ]] || fail "Fusion API changed or restarted"
echo "DIRECTOR_API_UNCHANGED=PASS"
echo "DB_REDIS_UNCHANGED=PASS"
echo "FACE_AUDIO_FUSION_UNCHANGED=PASS"

FINAL="$(run_state "$RUN_ID")"
IFS='|' read -r FINAL_STATE FINAL_ATTEMPTS FINAL_ERROR <<<"$FINAL"
echo
echo "============================================================"
echo " DEV DIRECTOR QUEUE RECOVERY PASS"
echo "============================================================"
echo "run_id=$RUN_ID"
echo "thread_id=$THREAD_ID"
echo "state=$FINAL_STATE"
echo "attempt_count=$FINAL_ATTEMPTS"
echo "DIRECTOR_QUEUE_CLAIM=PASS"
echo "DIRECTOR_API_UNCHANGED=PASS"
echo "DB_REDIS_UNCHANGED=PASS"
echo "FACE_AUDIO_FUSION_UNCHANGED=PASS"
echo "NEXT=REFRESH_DEV_MULTI_PERSON_PAGE"
