#!/usr/bin/env bash
set -euo pipefail

# Detached, restart-safe launcher/status wrapper for the canonical V3 Fusion
# stitch-only recovery. The long-running recovery is fully detached from SSH.

readonly WORKFLOW_ID="06c5d43e-7bbc-4cb4-aef3-9df36886da3b"
readonly FUSION_STAGE_ID="4038a526-308a-49ba-959a-7e40f512c3b3"
readonly EXPECTED_CHILDREN="28"
readonly CONFIRM_PHRASE="PAY 5.60 USD TO FINALIZE EXISTING 28 FUSION VIDEOS"
readonly RECOVERY_SCRIPT="scripts/v3-fusion-stitch-recovery-container-safe.sh"

POSTGRES_DB="${POSTGRES_DB:-desifaces_v3}"
POSTGRES_USER="${POSTGRES_USER:-desifaces_v3_admin}"
STATE_DIR="${DF_V3_FUSION_RECOVERY_STATE_DIR:-$HOME/.local/state/desifaces-v3/fusion-recovery}"
LOG_FILE="$STATE_DIR/recovery.log"
PID_FILE="$STATE_DIR/recovery.pid"
LOCK_FILE="$STATE_DIR/recovery.lock"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
umask 077

compose() { bash scripts/v3-compose.sh "$@"; }
psql_scalar() {
  compose exec -T desifaces-db \
    psql -X -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}
fail() { echo "ERROR: $*" >&2; exit 1; }

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

child_jobs_status_sql() {
  local wanted="$1"
  cat <<SQL
select count(*)
from public.studio_jobs j
where j.studio_type='fusion'
  and j.status='${wanted}'
  and (
    j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{billing_context,billing_parent_job_id}'='${FUSION_STAGE_ID}'
    or j.payload_json #>> '{pricing,parent_job_id}'='${FUSION_STAGE_ID}'
  )
SQL
}

read_state() {
  local latest_attempt_id
  latest_attempt_id="$(psql_scalar "select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"

  local stage_state attempt_no attempt_state phase parent_state latest_error children succeeded active_jobs consume_events credit_delta outputs pending_review
  stage_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  attempt_no="$(psql_scalar "select attempt_no from public.v3_studio_stage_attempts where attempt_id='${latest_attempt_id}'::uuid;")"
  attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where attempt_id='${latest_attempt_id}'::uuid;")"
  phase="$(psql_scalar "select coalesce(metadata_json #>> '{background_coordinator,phase}','') from public.v3_studio_stage_attempts where attempt_id='${latest_attempt_id}'::uuid;")"
  parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  latest_error="$(psql_scalar "select coalesce(error_message,'') from public.v3_studio_stage_attempts where attempt_id='${latest_attempt_id}'::uuid;")"
  children="$(psql_scalar "$(child_jobs_sql)")"
  succeeded="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
  active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
  consume_events="$(psql_scalar "select count(*) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
  credit_delta="$(psql_scalar "select coalesce(sum(l.credits_delta),0) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
  outputs="$(psql_scalar "select count(*) from public.v3_studio_stage_outputs where stage_run_id='${FUSION_STAGE_ID}'::uuid and is_active=true;")"
  pending_review="$(psql_scalar "select count(*) from public.v3_studio_review_items where stage_run_id='${FUSION_STAGE_ID}'::uuid and decision='pending';")"

  printf 'STAGE=%s\n' "$stage_state"
  printf 'ATTEMPT_NO=%s\n' "$attempt_no"
  printf 'ATTEMPT_ID=%s\n' "$latest_attempt_id"
  printf 'ATTEMPT_STATE=%s\n' "$attempt_state"
  printf 'PHASE=%s\n' "${phase:-unknown}"
  printf 'PARENT=%s\n' "$parent_state"
  printf 'PROVIDER_CHILDREN=%s\n' "$children"
  printf 'PROVIDER_CHILDREN_SUCCEEDED=%s\n' "$succeeded"
  printf 'ACTIVE_GENERATION_JOBS=%s\n' "$active_jobs"
  printf 'PARENT_CONSUME_EVENTS=%s\n' "$consume_events"
  printf 'PARENT_CREDIT_DELTA=%s\n' "$credit_delta"
  printf 'ACTIVE_OUTPUTS=%s\n' "$outputs"
  printf 'PENDING_REVIEW=%s\n' "$pending_review"
  printf 'ERROR=%s\n' "$latest_error"
}

worker() {
  local input_file="$1"

  exec 8>"$LOCK_FILE"
  if ! flock -n 8; then
    echo "$(date -Is) DUPLICATE_RECOVERY_BLOCKED=YES"
    exit 73
  fi

  [[ -f "$input_file" ]] || fail "detached recovery credential file missing before worker start"
  chmod 600 "$input_file"
  export DF_RECOVERY_INPUT_FILE="$input_file"

  echo "$(date -Is) DETACHED_RECOVERY_STARTED=YES"
  echo "$(date -Is) RECOVERY_SCRIPT=$RECOVERY_SCRIPT"
  echo "$(date -Is) DETACHED_AUTH_TRANSPORT=FILE_NOT_STDIN"

  set +e
  bash "$RECOVERY_SCRIPT" </dev/null
  rc=$?
  set -e

  rm -f "$input_file"
  unset DF_RECOVERY_INPUT_FILE || true

  echo "$(date -Is) DETACHED_RECOVERY_EXIT_CODE=$rc"
  rm -f "$PID_FILE"
  exit "$rc"
}

start() {
  [[ -f scripts/v3-compose.sh ]] || fail "run from ~/workspace/desifaces-v3"
  [[ -f "$RECOVERY_SCRIPT" ]] || fail "missing $RECOVERY_SCRIPT"
  [[ "$(git branch --show-current)" == "feature/v3-multiperson-core-20260818" ]] || fail "wrong branch"
  [[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 DB: $POSTGRES_DB"

  if [[ -f "$PID_FILE" ]]; then
    local existing_pid
    existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
      echo "RECOVERY_ALREADY_RUNNING=YES"
      echo "RECOVERY_PID=$existing_pid"
      echo "LOG_FILE=$LOG_FILE"
      exit 0
    fi
    rm -f "$PID_FILE"
  fi

  local active_jobs stage_state latest_attempt_id attempt_state parent_state latest_error children succeeded consume_events credit_delta
  active_jobs="$(psql_scalar "select count(*) from public.studio_jobs where status in ('queued','running','processing','submitted','pending','finalizing','pricing_pending');")"
  stage_state="$(psql_scalar "select state from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  latest_attempt_id="$(psql_scalar "select attempt_id::text from public.v3_studio_stage_attempts where stage_run_id='${FUSION_STAGE_ID}'::uuid order by attempt_no desc limit 1;")"
  attempt_state="$(psql_scalar "select state from public.v3_studio_stage_attempts where attempt_id='${latest_attempt_id}'::uuid;")"
  parent_state="$(psql_scalar "select coalesce(metadata_json->'fusion_parent_pricing'->>'state','') from public.v3_studio_stage_runs where stage_run_id='${FUSION_STAGE_ID}'::uuid;")"
  latest_error="$(psql_scalar "select coalesce(error_message,'') from public.v3_studio_stage_attempts where attempt_id='${latest_attempt_id}'::uuid;")"
  children="$(psql_scalar "$(child_jobs_sql)")"
  succeeded="$(psql_scalar "$(child_jobs_status_sql succeeded)")"
  consume_events="$(psql_scalar "select count(*) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"
  credit_delta="$(psql_scalar "select coalesce(sum(l.credits_delta),0) from public.pricing_credit_ledger_events l join public.v3_studio_workflows w on w.owner_user_id=l.user_id where w.workflow_id='${WORKFLOW_ID}'::uuid and l.idempotency_key like 'consume:svc-fusion-extension:v3-scene:${FUSION_STAGE_ID}:commit:%';")"

  [[ "$active_jobs" == "0" ]] || fail "active generation/pricing work exists"
  [[ "$stage_state" == "failed" ]] || fail "Fusion stage is not failed: $stage_state"
  [[ "$attempt_state" == "failed" ]] || fail "latest attempt is not failed: $attempt_state"
  [[ "$latest_error" == *"fusion_scene_stitch_failed:502"* ]] || fail "latest failure is not stitch-only"
  [[ "$parent_state" == "released" || "$parent_state" == "quoted" ]] || fail "parent state is not safely retryable: $parent_state"
  [[ "$children" == "$EXPECTED_CHILDREN" ]] || fail "provider child count changed: $children"
  [[ "$succeeded" == "$EXPECTED_CHILDREN" ]] || fail "provider children are not 28/28 succeeded: $succeeded"
  [[ "$consume_events" == "0" ]] || fail "parent already consumed"
  python3 - "$credit_delta" <<'PY'
from decimal import Decimal
import sys
assert Decimal(sys.argv[1]) == Decimal("0"), sys.argv[1]
PY

  echo "PRELAUNCH_DURABLE_STATE_GATE=PASS"
  echo "PROVIDER_CHILDREN_FROZEN=28/28"
  echo "PARENT_CONSUME_EVENTS=0"
  echo "PARENT_CREDIT_DELTA=0"

  local password confirmation input_file
  read -rsp "Enter test-account password: " password
  echo
  [[ -n "$password" ]] || fail "password must not be empty"

  read -r -p "Type exactly '$CONFIRM_PHRASE' to authorize the stitch-only retry: " confirmation
  [[ "$confirmation" == "$CONFIRM_PHRASE" ]] || {
    echo "PAYMENT_CONFIRMATION_NOT_ACCEPTED=STOP"
    unset password confirmation
    exit 0
  }

  input_file="$(mktemp "$STATE_DIR/recovery-input.XXXXXX")"
  chmod 600 "$input_file"
  printf '%s\n%s\n' "$password" "$confirmation" > "$input_file"
  unset password confirmation

  : > "$LOG_FILE"
  chmod 600 "$LOG_FILE"

  nohup setsid bash "$0" _worker "$input_file" >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  chmod 600 "$PID_FILE"

  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    echo "DETACHED_RECOVERY_LAUNCHED=YES"
    echo "RECOVERY_PID=$pid"
    echo "LOG_FILE=$LOG_FILE"
    echo "STATUS_COMMAND=bash $0 status"
    return 0
  fi

  echo "DETACHED_RECOVERY_LAUNCH_FAILED=YES"
  tail -40 "$LOG_FILE" 2>/dev/null || true
  exit 74
}

status() {
  echo "============================================================"
  echo " V3 FUSION DETACHED RECOVERY STATUS"
  echo "============================================================"

  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "DETACHED_RECOVERY_PROCESS=RUNNING"
      echo "RECOVERY_PID=$pid"
    else
      echo "DETACHED_RECOVERY_PROCESS=NOT_RUNNING"
    fi
  else
    echo "DETACHED_RECOVERY_PROCESS=NOT_RUNNING"
  fi

  read_state

  echo "LOG_FILE=$LOG_FILE"
  echo "---------------- LAST 30 LOG LINES ----------------"
  tail -30 "$LOG_FILE" 2>/dev/null || echo "NO_RECOVERY_LOG=YES"
}

case "${1:-status}" in
  start)
    start
    ;;
  status)
    status
    ;;
  _worker)
    [[ $# -eq 2 ]] || fail "worker invocation invalid"
    worker "$2"
    ;;
  *)
    echo "Usage: $0 {start|status}" >&2
    exit 64
    ;;
esac
