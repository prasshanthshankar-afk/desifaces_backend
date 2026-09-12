#!/usr/bin/env bash
set -euo pipefail

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly EXPECTED_ATTEMPT_NO="3"
readonly EXPECTED_CHILDREN="28"
readonly CANONICAL_CONTAINER="video-output"
readonly BRANCH="feature/v3-multiperson-core-20260818"

STATE_DIR="${DF_V3_FUSION_CLOSEOUT_STATE_DIR:-$HOME/.local/state/desifaces-v3/fusion-container-closeout}"
LOG_FILE="$STATE_DIR/closeout.log"
PID_FILE="$STATE_DIR/closeout.pid"
LOCK_FILE="$STATE_DIR/closeout.lock"
RESULT_FILE="$STATE_DIR/result.env"
RECOVERY_STATE_DIR="${DF_V3_FUSION_RECOVERY_STATE_DIR:-$HOME/.local/state/desifaces-v3/fusion-recovery}"
RECOVERY_PID_FILE="$RECOVERY_STATE_DIR/recovery.pid"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
umask 077

fail() { echo "ERROR: $*" >&2; exit 1; }
compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  docker exec -i desifaces-v3-db \
    psql -X -At -U desifaces_v3_admin -d desifaces_v3 -c "$1"
}

child_jobs_sql() {
  cat <<SQL
select count(*)
from public.studio_jobs j
where j.studio_type='fusion'
  and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  )
SQL
}

stop_recovery() {
  local pid=""
  if [[ -f "$RECOVERY_PID_FILE" ]]; then
    pid="$(cat "$RECOVERY_PID_FILE" 2>/dev/null || true)"
  fi

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "RECOVERY_PROCESS_FOUND=$pid"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$RECOVERY_PID_FILE"

  # Freeze only the stitch coordinator while we prove no retry was created.
  docker stop -t 15 df-v3-svc-fusion-extension-stitch-worker >/dev/null 2>&1 || true
  echo "RECOVERY_AND_STITCH_EXECUTION_FROZEN=PASS"
}

snapshot_gate() {
  local stage attempt_count latest_no latest_state children succeeded active consume delta artifact_count artifact_container
  stage="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  attempt_count="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  latest_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
  latest_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
  children="$(psql_scalar "$(child_jobs_sql)")"
  succeeded="$(psql_scalar "$(child_jobs_sql) and j.status='succeeded'")"
  active="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
  consume="$(psql_scalar "select count(*) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
  delta="$(psql_scalar "select coalesce(sum(l.credits_delta),0) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
  artifact_count="$(psql_scalar "select count(distinct split_part(split_part(c->>'video_url', '.blob.core.windows.net/', 2), '/', 1)) from public.v3_studio_stage_attempts a cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c where a.stage_run_id='${FUSION_STAGE_ID}'::uuid and coalesce(c->>'video_url','') like 'https://%.blob.core.windows.net/%';")"
  artifact_container="$(psql_scalar "select min(split_part(split_part(c->>'video_url', '.blob.core.windows.net/', 2), '/', 1)) from public.v3_studio_stage_attempts a cross join lateral jsonb_array_elements(coalesce(a.metadata_json->'children','[]'::jsonb)) c where a.stage_run_id='${FUSION_STAGE_ID}'::uuid and coalesce(c->>'video_url','') like 'https://%.blob.core.windows.net/%';")"

  printf 'SNAPSHOT_STAGE=%s\n' "$stage"
  printf 'SNAPSHOT_ATTEMPT_COUNT=%s\n' "$attempt_count"
  printf 'SNAPSHOT_LATEST_ATTEMPT_NO=%s\n' "$latest_no"
  printf 'SNAPSHOT_LATEST_ATTEMPT_STATE=%s\n' "$latest_state"
  printf 'SNAPSHOT_PROVIDER_CHILDREN=%s\n' "$children"
  printf 'SNAPSHOT_PROVIDER_CHILDREN_SUCCEEDED=%s\n' "$succeeded"
  printf 'SNAPSHOT_ACTIVE_JOBS=%s\n' "$active"
  printf 'SNAPSHOT_PARENT_CONSUME_EVENTS=%s\n' "$consume"
  printf 'SNAPSHOT_PARENT_CREDIT_DELTA=%s\n' "$delta"
  printf 'SNAPSHOT_ARTIFACT_CONTAINER=%s\n' "$artifact_container"

  [[ "$stage" == "failed" ]] || fail "workflow mutated before storage closeout: stage=$stage"
  [[ "$attempt_count" == "$EXPECTED_ATTEMPT_NO" ]] || fail "workflow mutated before storage closeout: attempt_count=$attempt_count"
  [[ "$latest_no" == "$EXPECTED_ATTEMPT_NO" ]] || fail "workflow mutated before storage closeout: latest_attempt_no=$latest_no"
  [[ "$latest_state" == "failed" ]] || fail "workflow mutated before storage closeout: latest_state=$latest_state"
  [[ "$children" == "$EXPECTED_CHILDREN" ]] || fail "provider child count changed: $children"
  [[ "$succeeded" == "$EXPECTED_CHILDREN" ]] || fail "provider success count changed: $succeeded"
  [[ "$active" == "0" ]] || fail "active generation/pricing jobs exist: $active"
  [[ "$consume" == "0" ]] || fail "parent consume event exists: $consume"
  python3 - "$delta" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal("0"), sys.argv[1]
PY
  [[ "$artifact_count" == "1" ]] || fail "preserved artifacts span $artifact_count Azure containers"
  [[ "$artifact_container" == "$CANONICAL_CONTAINER" ]] || fail "preserved artifacts use $artifact_container, expected $CANONICAL_CONTAINER"

  echo "PRE_REMEDIATION_SAFETY_GATE=PASS"
}

verify_container_env() {
  local name="$1"
  local video final
  video="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$name" | sed -n 's/^AZURE_VIDEO_OUTPUT_CONTAINER=//p' | tail -1)"
  final="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$name" | sed -n 's/^AZURE_FINAL_VIDEO_CONTAINER=//p' | tail -1)"
  [[ "$video" == "$CANONICAL_CONTAINER" ]] || fail "$name AZURE_VIDEO_OUTPUT_CONTAINER=$video"
  [[ "$final" == "$CANONICAL_CONTAINER" ]] || fail "$name AZURE_FINAL_VIDEO_CONTAINER=$final"
  echo "LIVE_CONTAINER_CONTRACT_${name}=PASS:$video"
}

azure_probe() {
  docker exec -i \
    -e DF_EXPECTED_CONTAINER="$CANONICAL_CONTAINER" \
    df-v3-svc-fusion-extension-stitch-worker \
    python - <<'PY'
import os
import uuid
from azure.storage.blob import BlobServiceClient
from app.config import settings

expected = os.environ["DF_EXPECTED_CONTAINER"]
video = str(settings.AZURE_VIDEO_OUTPUT_CONTAINER or "").strip()
final = str(settings.AZURE_FINAL_VIDEO_CONTAINER or "").strip()
assert video == expected, (video, expected)
assert final == expected, (final, expected)
service = BlobServiceClient.from_connection_string(settings.AZURE_STORAGE_CONNECTION_STRING)
container = service.get_container_client(expected)
props = container.get_container_properties()
blob_name = f"_health/v3-fusion-container-closeout-{uuid.uuid4().hex}.txt"
blob = container.get_blob_client(blob_name)
try:
    blob.upload_blob(b"desifaces-v3-container-closeout\n", overwrite=True)
    assert blob.exists()
finally:
    try:
        blob.delete_blob()
    except Exception:
        pass
print(f"AZURE_STORAGE_ACCOUNT={service.account_name}")
print(f"AZURE_CONTAINER={expected}")
print(f"AZURE_CONTAINER_ETAG={props.etag}")
print("AZURE_CONTAINER_RESOLVE=PASS")
print("AZURE_CONTAINER_WRITE_DELETE=PASS")
PY
}

final_invariants() {
  local attempt_count latest_no children succeeded consume delta active
  attempt_count="$(psql_scalar "select count(*) from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  latest_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
  children="$(psql_scalar "$(child_jobs_sql)")"
  succeeded="$(psql_scalar "$(child_jobs_sql) and j.status='succeeded'")"
  consume="$(psql_scalar "select count(*) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
  delta="$(psql_scalar "select coalesce(sum(l.credits_delta),0) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
  active="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"

  [[ "$attempt_count" == "$EXPECTED_ATTEMPT_NO" ]] || fail "closeout created/observed new attempt: $attempt_count"
  [[ "$latest_no" == "$EXPECTED_ATTEMPT_NO" ]] || fail "latest attempt changed: $latest_no"
  [[ "$children" == "$EXPECTED_CHILDREN" && "$succeeded" == "$EXPECTED_CHILDREN" ]] || fail "provider lineage changed"
  [[ "$consume" == "0" ]] || fail "pricing consume changed: $consume"
  python3 - "$delta" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal("0"), sys.argv[1]
PY
  [[ "$active" == "0" ]] || fail "active jobs appeared during closeout: $active"

  echo "NO_FUSION_RETRY_CREATED=PASS"
  echo "NO_PROVIDER_RERENDER=PASS"
  echo "NO_PRICING_CONSUME=PASS"
}

worker() {
  exec 8>"$LOCK_FILE"
  if ! flock -n 8; then
    echo "CLOSEOUT_DUPLICATE_BLOCKED=YES"
    exit 73
  fi

  : > "$RESULT_FILE"
  echo "$(date -Is) CONTAINER_CLOSEOUT_STARTED=YES"

  stop_recovery
  snapshot_gate

  export DF_V3_FUSION_OUTPUT_CONTAINER="$CANONICAL_CONTAINER"

  echo "RECREATE_WITHOUT_BUILD=STARTED"
  compose --profile v3-execution up -d --no-deps --no-build --force-recreate \
    svc-fusion svc-fusion-extension svc-fusion-extension-stitch-worker
  echo "RECREATE_WITHOUT_BUILD=PASS"

  for name in df-v3-svc-fusion df-v3-svc-fusion-extension df-v3-svc-fusion-extension-stitch-worker; do
    for _ in $(seq 1 30); do
      [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" == "true" ]] && break
      sleep 1
    done
    [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)" == "true" ]] || fail "$name not running"
    verify_container_env "$name"
  done
  echo "LIVE_CONTAINER_CONTRACT_GATE=PASS"

  azure_probe
  final_invariants

  {
    echo "CONTAINER_NOT_FOUND_ROOT_CAUSE_RESOLVED=PASS"
    echo "CANONICAL_AZURE_CONTAINER=$CANONICAL_CONTAINER"
    echo "WORKFLOW_RETRY_ALLOWED=NO"
  } | tee "$RESULT_FILE"

  echo "$(date -Is) CONTAINER_CLOSEOUT_COMPLETED=PASS"
  rm -f "$PID_FILE"
}

start() {
  [[ "$(git branch --show-current)" == "$BRANCH" ]] || fail "wrong branch"
  [[ -f scripts/v3-compose.sh ]] || fail "run from desifaces-v3 workspace"

  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "CONTAINER_CLOSEOUT_ALREADY_RUNNING=YES"
      echo "CLOSEOUT_PID=$pid"
      echo "STATUS_COMMAND=bash $0 status"
      exit 0
    fi
    rm -f "$PID_FILE"
  fi

  : > "$LOG_FILE"
  chmod 600 "$LOG_FILE"
  nohup setsid bash "$0" _worker >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  chmod 600 "$PID_FILE"
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    echo "CONTAINER_CLOSEOUT_LAUNCHED=YES"
    echo "CLOSEOUT_PID=$pid"
    echo "LOG_FILE=$LOG_FILE"
    echo "STATUS_COMMAND=bash $0 status"
  else
    echo "CONTAINER_CLOSEOUT_LAUNCH_FAILED=YES"
    tail -50 "$LOG_FILE" || true
    exit 74
  fi
}

status() {
  echo "============================================================"
  echo " V3 FUSION CONTAINER CLOSEOUT STATUS"
  echo "============================================================"
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "CONTAINER_CLOSEOUT_PROCESS=RUNNING"
      echo "CLOSEOUT_PID=$pid"
    else
      echo "CONTAINER_CLOSEOUT_PROCESS=NOT_RUNNING"
    fi
  else
    echo "CONTAINER_CLOSEOUT_PROCESS=NOT_RUNNING"
  fi

  if [[ -s "$RESULT_FILE" ]]; then
    echo "---------------- RESULT ----------------"
    cat "$RESULT_FILE"
  fi
  echo "---------------- LAST 40 LOG LINES ----------------"
  tail -40 "$LOG_FILE" 2>/dev/null || echo "NO_CLOSEOUT_LOG=YES"
}

case "${1:-status}" in
  start) start ;;
  status) status ;;
  _worker) worker ;;
  *) echo "Usage: $0 {start|status}" >&2; exit 64 ;;
esac
