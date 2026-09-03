#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORKFLOW_ID="${WORKFLOW_ID:-}"
STAGE_ID="${STAGE_ID:-}"
DF_EMAIL="${DF_EMAIL:-}"
DIRECTOR_BASE="${DIRECTOR_BASE:-http://127.0.0.1:18011}"
PRICING_BASE="${PRICING_BASE:-http://127.0.0.1:18009}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
POLL_SECONDS="${POLL_SECONDS:-5}"
MAX_POLLS="${MAX_POLLS:-240}"
HELPER_NAME="${HELPER_NAME:-df-v3-svc-fusion-worker-parallel-resume}"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")

fail() { echo "V3 FUSION RESUME: HOLD: $*" >&2; exit 1; }
info() { echo; echo "===== $* ====="; }

[[ -n "$WORKFLOW_ID" ]] || fail "WORKFLOW_ID is required"
[[ -n "$DF_EMAIL" ]] || fail "DF_EMAIL is required"
[[ -f "$ROOT/infra/.env" ]] || fail "missing infra/.env"

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 database"

psql_at() {
  "${COMPOSE[@]}" exec -T desifaces-db \
    psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

if [[ -z "$STAGE_ID" ]]; then
  STAGE_ID="$(psql_at "select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and stage_type='fusion' and scope_type='scene' and state<>'approved' order by created_at desc limit 1")"
fi
[[ -n "$STAGE_ID" ]] || fail "Fusion stage not found"

USER_ID="$(psql_at "select owner_user_id::text from public.v3_studio_workflows where workflow_id='$WORKFLOW_ID'::uuid")"
[[ -n "$USER_ID" ]] || fail "workflow owner missing"

stage_job_count() {
  psql_at "select count(*) from public.studio_jobs where studio_type='fusion' and user_id='$USER_ID'::uuid and (payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='$STAGE_ID' or payload_json #>> '{tags,billing_context,billing_parent_job_id}'='$STAGE_ID')"
}

stage_status_counts() {
  psql_at "select count(*) filter(where status='queued'),count(*) filter(where status in ('running','processing')),count(*) filter(where status='succeeded'),count(*) filter(where status in ('failed','blocked','canceled','cancelled')) from public.studio_jobs where studio_type='fusion' and user_id='$USER_ID'::uuid and (payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='$STAGE_ID' or payload_json #>> '{tags,billing_context,billing_parent_job_id}'='$STAGE_ID')"
}

latest_reservation() {
  psql_at "select id::text,status,reserved_credits from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID' order by created_at desc limit 1"
}

info "1. EXISTING ATTEMPT / ECONOMIC STATE"
JOBS_BEFORE="$(stage_job_count)"
STATUS_BEFORE="$(stage_status_counts)"
RES_BEFORE="$(latest_reservation)"
echo "workflow_id=$WORKFLOW_ID"
echo "stage_id=$STAGE_ID"
echo "existing_internal_children=$JOBS_BEFORE"
echo "queued|active|succeeded|failed=$STATUS_BEFORE"
echo "latest_parent_reservation=$RES_BEFORE"
[[ "$JOBS_BEFORE" -gt 0 ]] || fail "no existing child jobs; refusing to create any"

info "2. BUILD PARALLEL FUSION WORKER"
python3 -m py_compile services/svc-fusion/app/app/workers/fusion_worker.py
"${COMPOSE[@]}" build svc-fusion-worker

IFS='|' read -r Q0 A0 S0 F0 <<< "$STATUS_BEFORE"
if [[ "${Q0:-0}" -gt 0 ]]; then
  info "3. START ADDITIVE PARALLEL WORKER FOR ALREADY-QUEUED JOBS"
  if docker ps -a --format '{{.Names}}' | grep -qx "$HELPER_NAME"; then
    if ! docker ps --format '{{.Names}}' | grep -qx "$HELPER_NAME"; then
      docker start "$HELPER_NAME" >/dev/null
    fi
  else
    "${COMPOSE[@]}" run -d --no-deps \
      --name "$HELPER_NAME" \
      -e DF_FUSION_WORKER_CONCURRENCY=32 \
      svc-fusion-worker >/dev/null
  fi
  echo "parallel_helper=$HELPER_NAME"
else
  info "3. NO QUEUED CHILDREN REMAIN"
  echo "parallel_helper=not_needed"
fi

info "4. AUTH WITH AUTOMATIC REFRESH"
read -rsp "Enter test-account password: " DF_PASS_INPUT
echo

refresh_auth() {
  local exports rc
  export DF_PASSWORD="$DF_PASS_INPUT"
  exports="$(CORE_URL="$CORE_URL" DF_EMAIL="$DF_EMAIL" python3 scripts/df_login_exports.py)"
  rc=$?
  unset DF_PASSWORD
  [[ "$rc" == "0" ]] || return "$rc"
  eval "$exports"
  export DF_AUTH_TOKEN="$DF_BEARER_TOKEN"
  return 0
}

refresh_auth || fail "login failed"
echo "LOGIN = PASS"

info "5. RESUME SYNC ON EXISTING ATTEMPT ONLY"
TMP="$(mktemp)"
trap 'rm -f "$TMP"; unset DF_PASS_INPUT DF_PASSWORD 2>/dev/null || true' EXIT
SUCCESS=0

for i in $(seq 1 "$MAX_POLLS"); do
  CODE="$(curl -sS -o "$TMP" -w '%{http_code}' \
    -X POST -H "Authorization: Bearer $DF_AUTH_TOKEN" \
    -H 'Content-Type: application/json' \
    "$DIRECTOR_BASE/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/sync" \
    -d '{}')"

  if [[ "$CODE" == "401" ]]; then
    echo "poll $i: auth refresh"
    refresh_auth || fail "token refresh login failed"
    CODE="$(curl -sS -o "$TMP" -w '%{http_code}' \
      -X POST -H "Authorization: Bearer $DF_AUTH_TOKEN" \
      -H 'Content-Type: application/json' \
      "$DIRECTOR_BASE/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/sync" \
      -d '{}')"
  fi

  [[ "$CODE" == "200" ]] || { cat "$TMP"; fail "sync HTTP $CODE"; }

  STAGE_STATE="$(jq -r '.stage_state // ""' "$TMP")"
  PROVIDER_STATE="$(jq -r '.provider_state // ""' "$TMP")"
  PSTATE="$(jq -r '.parent_pricing.state // ""' "$TMP")"
  DONE="$(jq -r '[.children[]? | select((.status//"")|ascii_downcase|IN("succeeded","completed","complete","ready"))] | length' "$TMP")"
  TOTAL="$(jq -r '.children | length' "$TMP")"
  QUEUED="$(jq -r '[.children[]? | select((.status//"")|ascii_downcase=="queued")] | length' "$TMP")"
  ACTIVE="$(jq -r '[.children[]? | select((.status//"")|ascii_downcase|IN("running","processing","submitted","pending","finalizing","unknown",""))] | length' "$TMP")"
  echo "poll $i: stage=$STAGE_STATE provider=$PROVIDER_STATE completed=$DONE/$TOTAL active=$ACTIVE queued=$QUEUED pricing=$PSTATE"

  if [[ "$STAGE_STATE" == "awaiting_review" && "$PSTATE" == "committed" ]]; then
    SUCCESS=1
    break
  fi
  if [[ "$STAGE_STATE" == "failed" ]]; then
    cat "$TMP"
    fail "existing attempt entered failed state"
  fi
  sleep "$POLL_SECONDS"
done

[[ "$SUCCESS" == "1" ]] || fail "existing attempt did not reach committed awaiting_review"

info "6. NO-DUPLICATE + ECONOMIC RECONCILIATION"
JOBS_AFTER="$(stage_job_count)"
RES_AFTER_COUNT="$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")"
NEG_COUNT="$(psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'consume:svc-fusion-extension:v3-scene:$STAGE_ID:commit:%' and credits_delta<0")"
LEDGER="$(psql_at "select credits_delta,coalesce(sku_code,''),coalesce(quantity::text,'') from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'consume:svc-fusion-extension:v3-scene:$STAGE_ID:commit:%' and credits_delta<0 order by created_at desc limit 1")"

echo "stage_jobs_before=$JOBS_BEFORE"
echo "stage_jobs_after=$JOBS_AFTER"
echo "parent_reservations_total=$RES_AFTER_COUNT"
echo "negative_scene_ledger_events=$NEG_COUNT"
echo "latest_scene_charge=$LEDGER"
[[ "$JOBS_AFTER" == "$JOBS_BEFORE" ]] || fail "resume created new Fusion child jobs"
[[ "$NEG_COUNT" == "1" ]] || fail "expected exactly one logical scene charge"

curl -fsS -H "Authorization: Bearer $DF_AUTH_TOKEN" "$PRICING_BASE/api/credits/balance" | jq .

echo
cat "$TMP" | jq '{stage_state,provider_state,media_asset_id,review_item_id,parent_pricing:{state:.parent_pricing.state,reservation_id:.parent_pricing.reservation_id,ledger_entry_id:.parent_pricing.ledger_entry_id},children_completed:([.children[]?|select((.status//"")|ascii_downcase|IN("succeeded","completed","complete","ready"))]|length),children_total:(.children|length)}'

echo
echo "============================================================"
echo " V3 FUSION EXISTING ATTEMPT RESUME = PASS"
echo " NO REDISPATCH / NO NEW CHILD JOBS / ONE PARENT CHARGE"
echo "============================================================"
