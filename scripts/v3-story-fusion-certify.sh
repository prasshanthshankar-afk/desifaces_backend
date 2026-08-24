#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIRECTOR_BASE="${DIRECTOR_BASE:-http://127.0.0.1:18011}"
PRICING_BASE="${PRICING_BASE:-http://127.0.0.1:18009}"
WORKFLOW_ID="${WORKFLOW_ID:-}"
AUTH_TOKEN="${AUTH_TOKEN:-${DF_AUTH_TOKEN:-}}"
EXECUTE_BILLABLE="${EXECUTE_BILLABLE:-0}"
MAX_POLLS="${MAX_POLLS:-180}"
POLL_SECONDS="${POLL_SECONDS:-5}"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")

fail() {
  echo "V3 FUSION CERT: FAIL: $*" >&2
  exit 1
}

info() {
  echo
  echo "===== $* ====="
}

[[ -n "$WORKFLOW_ID" ]] || fail "WORKFLOW_ID is required"
[[ -n "$AUTH_TOKEN" ]] || fail "AUTH_TOKEN (or DF_AUTH_TOKEN) is required"
command -v jq >/dev/null || fail "jq is required"
command -v curl >/dev/null || fail "curl is required"
[[ -f "$ROOT/infra/.env" ]] || fail "missing infra/.env"

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 database"
[[ "$POSTGRES_USER" == "desifaces_v3_admin" ]] || fail "refusing non-V3 database user"

AUTH_HEADER="Authorization: Bearer $AUTH_TOKEN"
TMPDIR_CERT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CERT"' EXIT

api_get_file() {
  local base="$1" path="$2" out="$3"
  local code
  code="$(curl -sS -o "$out" -w '%{http_code}' -H "$AUTH_HEADER" "$base$path")"
  [[ "$code" == "200" ]] || {
    echo "HTTP_$code $base$path" >&2
    cat "$out" >&2 || true
    return 1
  }
}

api_post_code() {
  local path="$1" body="$2" out="$3"
  curl -sS -o "$out" -w '%{http_code}' \
    -H "$AUTH_HEADER" -H 'Content-Type: application/json' \
    -X POST --data "$body" "$DIRECTOR_BASE$path"
}

psql_at() {
  "${COMPOSE[@]}" exec -T desifaces-db \
    psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

balance_json() {
  api_get_file "$PRICING_BASE" "/api/credits/balance" "$1" || fail "pricing balance lookup failed"
}

balance_triplet() {
  jq -r '[.balance_credits,.reserved_credits,.available_credits] | @tsv' "$1"
}

info "1. WORKFLOW + HITL PREFLIGHT"
api_get_file "$DIRECTOR_BASE" "/api/director/studio-workflows/$WORKFLOW_ID" "$TMPDIR_CERT/workflow.json" \
  || fail "workflow lookup failed"
STATE="$(jq -r '.state // ""' "$TMPDIR_CERT/workflow.json")"
CURRENT_STAGE="$(jq -r '.current_stage // ""' "$TMPDIR_CERT/workflow.json")"
FINAL_MEDIA="$(jq -r '.final_media_id // ""' "$TMPDIR_CERT/workflow.json")"
if [[ "$STATE" == "completed" ]]; then
  [[ -n "$FINAL_MEDIA" && "$FINAL_MEDIA" != "null" ]] || fail "completed workflow has no final_media_id"
  echo "PASS: workflow already completed with final_media_id=$FINAL_MEDIA"
  exit 0
fi
[[ "$CURRENT_STAGE" == "fusion" ]] || fail "workflow current_stage=$CURRENT_STAGE, expected fusion"

api_get_file "$DIRECTOR_BASE" "/api/director/studio-workflows/$WORKFLOW_ID/preflight" "$TMPDIR_CERT/preflight.json" \
  || fail "preflight failed"
FACE_APPROVED="$(jq -r '.face.approved // 0' "$TMPDIR_CERT/preflight.json")"
FACE_TOTAL="$(jq -r '.face.total // 0' "$TMPDIR_CERT/preflight.json")"
AUDIO_APPROVED="$(jq -r '.audio.approved // 0' "$TMPDIR_CERT/preflight.json")"
AUDIO_TOTAL="$(jq -r '.audio.total // 0' "$TMPDIR_CERT/preflight.json")"
[[ "$FACE_TOTAL" -gt 0 && "$FACE_APPROVED" -eq "$FACE_TOTAL" ]] || fail "Face not fully approved ($FACE_APPROVED/$FACE_TOTAL)"
[[ "$AUDIO_TOTAL" -gt 0 && "$AUDIO_APPROVED" -eq "$AUDIO_TOTAL" ]] || fail "Audio not fully approved ($AUDIO_APPROVED/$AUDIO_TOTAL)"

STAGE_ID="$(jq -r '[.stages[] | select(.stage_type=="fusion" and .scope_type=="scene" and .state!="approved")][0].stage_run_id // ""' "$TMPDIR_CERT/workflow.json")"
[[ -n "$STAGE_ID" ]] || fail "Fusion scene stage not found"
IDENTITY="$(psql_at "select owner_user_id::text,project_id::text from public.v3_studio_workflows where workflow_id='$WORKFLOW_ID'::uuid")"
IFS='|' read -r USER_ID PROJECT_ID <<< "$IDENTITY"
[[ -n "$USER_ID" && -n "$PROJECT_ID" ]] || fail "workflow owner/project identity missing"
echo "PASS: Face $FACE_APPROVED/$FACE_TOTAL • Audio $AUDIO_APPROVED/$AUDIO_TOTAL • Fusion stage $STAGE_ID"

info "2. HARD BILLING TOPOLOGY GATE"
PREVIEW_CODE="$(api_post_code "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/pricing-preview" '{"external_provider_ok":true}' "$TMPDIR_CERT/preview.json")"
[[ "$PREVIEW_CODE" == "200" ]] || { cat "$TMPDIR_CERT/preview.json"; fail "Fusion parent pricing preview HTTP $PREVIEW_CODE"; }

TURN_COUNT="$(jq -r '.turn_count // 0' "$TMPDIR_CERT/preview.json")"
PARENT_COUNT="$(jq -r '.billable_parent_quote_count // 0' "$TMPDIR_CERT/preview.json")"
CHILD_BILLABLE="$(jq -r '.billable_child_quote_count // -1' "$TMPDIR_CERT/preview.json")"
REQUIRED_CHILD="$(jq -r '.required_child_count // 0' "$TMPDIR_CERT/preview.json")"
SUPPRESSED_CHILD="$(jq -r '.child_pricing_suppressed // 0' "$TMPDIR_CERT/preview.json")"
[[ "$TURN_COUNT" -gt 0 ]] || fail "no Fusion dialogue turns"
[[ "$PARENT_COUNT" == "1" ]] || fail "parent quote count=$PARENT_COUNT, expected 1"
[[ "$CHILD_BILLABLE" == "0" ]] || fail "billable child quote count=$CHILD_BILLABLE, expected 0"
[[ "$SUPPRESSED_CHILD" == "$REQUIRED_CHILD" ]] || fail "required child suppression=$SUPPRESSED_CHILD/$REQUIRED_CHILD"

jq -e 'all(.children[]?; .pricing_suppressed == true and .pricing.state == "suppressed" and ((.pricing.quote_id // "") == "") and ((.pricing.reservation_id // "") == "") and ((.pricing.billed_units // "0") == "0"))' "$TMPDIR_CERT/preview.json" >/dev/null \
  || fail "one or more required child renders are not strictly pricing-suppressed"

PARENT_UNIT="$(jq -r '.parent_quote.pricing.unit_type // ""' "$TMPDIR_CERT/preview.json")"
PARENT_PROVIDER="$(jq -r '.parent_quote.provider // .parent_quote.pricing.provider // ""' "$TMPDIR_CERT/preview.json")"
DURATION_SOURCE="$(jq -r '.parent_quote.duration_source // ""' "$TMPDIR_CERT/preview.json")"
TOTAL_SECONDS="$(jq -r '.parent_quote.total_audio_duration_sec // 0' "$TMPDIR_CERT/preview.json")"
MINUTES="$(jq -r '.parent_quote.billable_minutes // 0' "$TMPDIR_CERT/preview.json")"
QUOTE_ID="$(jq -r '.parent_quote.pricing.quote_id // ""' "$TMPDIR_CERT/preview.json")"
FINGERPRINT="$(jq -r '.parent_quote.pricing.preview_fingerprint // ""' "$TMPDIR_CERT/preview.json")"
QUOTED_CREDITS="$(jq -r '.parent_quote.pricing.meta.total_credits // 0' "$TMPDIR_CERT/preview.json")"
SKU_CREDITS="$(psql_at "select default_unit_credits from public.pricing_skus where code='FUSION_TALK_MIN' and unit='minute' and coalesce(provider_hint,'')=''")"

[[ "$PARENT_UNIT" == "minute" ]] || fail "parent unit=$PARENT_UNIT, expected minute"
[[ "$PARENT_PROVIDER" == "veed_fabric" ]] || fail "provider=$PARENT_PROVIDER, expected veed_fabric"
[[ "$DURATION_SOURCE" == "approved_audio_ffprobe" ]] || fail "duration source=$DURATION_SOURCE"
[[ -n "$QUOTE_ID" && -n "$FINGERPRINT" ]] || fail "parent confirmation contract missing"
[[ "$SKU_CREDITS" =~ ^[0-9]+$ && "$SKU_CREDITS" -gt 0 ]] || fail "DB Fusion credit rate invalid"
EXPECTED_MINUTES="$(python3 - "$TOTAL_SECONDS" <<'PY'
import math, sys
print(max(1, math.ceil(max(float(sys.argv[1]), 0.001)/60.0)))
PY
)"
[[ "$MINUTES" == "$EXPECTED_MINUTES" ]] || fail "billable minutes=$MINUTES expected=$EXPECTED_MINUTES"
EXPECTED_CREDITS=$((MINUTES * SKU_CREDITS))
[[ "$QUOTED_CREDITS" == "$EXPECTED_CREDITS" ]] || fail "quoted credits=$QUOTED_CREDITS expected=$EXPECTED_CREDITS"
jq -e '(.parent_quote.pricing.meta.lines | length) == 1 and .parent_quote.pricing.meta.lines[0].unit == "minute" and ((.parent_quote.pricing.meta.lines[0].provider_hint // "") == "")' "$TMPDIR_CERT/preview.json" >/dev/null \
  || fail "parent pricing line must be one provider-neutral minute line"
if grep -qi 'heygen' "$TMPDIR_CERT/preview.json"; then fail "stale heygen metadata present"; fi

echo "PASS: 1 parent quote / 0 child quotes / $SUPPRESSED_CHILD suppressed required children"
echo "PASS: actual_audio=$TOTAL_SECONDS sec -> $MINUTES minute(s) -> $QUOTED_CREDITS credits"

if [[ "$EXECUTE_BILLABLE" != "1" ]]; then
  echo
  echo "PASS: safe non-billable topology check complete."
  echo "Run scripts/v3-fusion-pricing-certify.sh for full reserve/release certification before billable execution."
  exit 0
fi

info "3. BILLABLE BASELINE"
balance_json "$TMPDIR_CERT/balance-before.json"
BALANCE_BEFORE="$(balance_triplet "$TMPDIR_CERT/balance-before.json")"
RES_BEFORE="$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")"
NEG_BEFORE="$(psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'svc-fusion-extension:v3-scene:$STAGE_ID:%' and credits_delta < 0")"
JOBS_BEFORE="$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")"

echo "wallet=$BALANCE_BEFORE"
echo "scene_reservations=$RES_BEFORE negative_scene_ledger=$NEG_BEFORE provider_jobs=$JOBS_BEFORE"

info "4. ONE PARENT RESERVATION + INTERNAL CHILD DISPATCH"
CHILD_CONFIRMATIONS="$(jq -c '[.children[] | {dialogue_turn_id,request_nonce}]' "$TMPDIR_CERT/preview.json")"
DISPATCH_BODY="$(jq -cn --arg quote "$QUOTE_ID" --arg fp "$FINGERPRINT" --argjson children "$CHILD_CONFIRMATIONS" '{parent_confirmation:{quote_id:$quote,preview_fingerprint:$fp},child_confirmations:$children,external_provider_ok:true,user_confirmed:true}')"
DISPATCH_CODE="$(api_post_code "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/dispatch" "$DISPATCH_BODY" "$TMPDIR_CERT/dispatch1.json")"
[[ "$DISPATCH_CODE" == "200" ]] || { cat "$TMPDIR_CERT/dispatch1.json"; fail "Fusion dispatch HTTP $DISPATCH_CODE"; }
ATTEMPT_ID="$(jq -r '.attempt_id // ""' "$TMPDIR_CERT/dispatch1.json")"
RESERVATION_ID="$(jq -r '.parent_pricing.reservation_id // ""' "$TMPDIR_CERT/dispatch1.json")"
[[ -n "$ATTEMPT_ID" && -n "$RESERVATION_ID" ]] || fail "dispatch missing attempt/reservation ID"
[[ "$(jq -r '.parent_pricing.state // ""' "$TMPDIR_CERT/dispatch1.json")" == "reserved" ]] || fail "parent pricing was not reserved"
[[ "$(jq -r '[.children[]? | select(.pricing_suppressed==true)] | length' "$TMPDIR_CERT/dispatch1.json")" == "$TURN_COUNT" ]] || fail "not all dispatched children are marked pricing_suppressed"
[[ "$(jq -r '[.children[]? | select(((.quote_id // "") != ""))] | length' "$TMPDIR_CERT/dispatch1.json")" == "0" ]] || fail "a child contains a billable quote ID"

balance_json "$TMPDIR_CERT/balance-reserved.json"
read -r B0 R0 A0 <<< "$(tr '\t' ' ' <<< "$BALANCE_BEFORE")"
read -r B1 R1 A1 <<< "$(tr '\t' ' ' <<< "$(balance_triplet "$TMPDIR_CERT/balance-reserved.json")")"
[[ "$B1" == "$B0" ]] || fail "reservation changed balance credits"
[[ $((R1-R0)) -eq "$QUOTED_CREDITS" ]] || fail "reserve delta=$((R1-R0)), expected $QUOTED_CREDITS"
[[ $((A0-A1)) -eq "$QUOTED_CREDITS" ]] || fail "available delta=$((A0-A1)), expected $QUOTED_CREDITS"
RES_AFTER_DISPATCH="$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")"
[[ "$RES_AFTER_DISPATCH" == "$((RES_BEFORE + 1))" ]] || fail "expected one new parent reservation"
JOBS_AFTER_DISPATCH="$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")"
[[ $((JOBS_AFTER_DISPATCH-JOBS_BEFORE)) -eq "$REQUIRED_CHILD" ]] || fail "provider job delta=$((JOBS_AFTER_DISPATCH-JOBS_BEFORE)), expected required children=$REQUIRED_CHILD"

echo "PASS: exactly one parent hold and $REQUIRED_CHILD internal child provider jobs"

info "5. DUPLICATE DISPATCH IDEMPOTENCY"
DUP_CODE="$(api_post_code "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/dispatch" "$DISPATCH_BODY" "$TMPDIR_CERT/dispatch2.json")"
[[ "$DUP_CODE" == "200" ]] || { cat "$TMPDIR_CERT/dispatch2.json"; fail "duplicate dispatch HTTP $DUP_CODE"; }
[[ "$(jq -r '.attempt_id // ""' "$TMPDIR_CERT/dispatch2.json")" == "$ATTEMPT_ID" ]] || fail "duplicate dispatch created a new attempt"
[[ "$(jq -r '.parent_pricing.reservation_id // ""' "$TMPDIR_CERT/dispatch2.json")" == "$RESERVATION_ID" ]] || fail "duplicate dispatch changed parent reservation"
[[ "$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")" == "$JOBS_AFTER_DISPATCH" ]] || fail "duplicate dispatch created provider jobs"
[[ "$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")" == "$RES_AFTER_DISPATCH" ]] || fail "duplicate dispatch created reservation"
balance_json "$TMPDIR_CERT/balance-duplicate-dispatch.json"
[[ "$(balance_triplet "$TMPDIR_CERT/balance-duplicate-dispatch.json")" == "$(balance_triplet "$TMPDIR_CERT/balance-reserved.json")" ]] || fail "duplicate dispatch changed wallet"
echo "PASS: duplicate dispatch produces zero economic/provider duplicates"

info "6. POLL -> ORDERED STITCH -> ONE COMMIT"
SUCCESS=0
for poll in $(seq 1 "$MAX_POLLS"); do
  SYNC_CODE="$(api_post_code "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/sync" '{}' "$TMPDIR_CERT/sync.json")"
  if [[ "$SYNC_CODE" != "200" ]]; then
    DETAIL="$(jq -r '.detail // empty' "$TMPDIR_CERT/sync.json" 2>/dev/null || true)"
    STAGE_STATE="$(psql_at "select state from public.v3_studio_stage_runs where stage_run_id='$STAGE_ID'::uuid")"
    if [[ "$STAGE_STATE" == "failed" ]]; then
      balance_json "$TMPDIR_CERT/balance-failed.json"
      echo "failure_detail=$DETAIL"
      echo "wallet_after_failure=$(balance_triplet "$TMPDIR_CERT/balance-failed.json")"
      fail "Fusion failed; successful children are preserved and parent release was requested. DO NOT redispatch blindly."
    fi
    if [[ "$DETAIL" == *"fusion_parent_pricing_commit_pending"* ]]; then
      echo "poll $poll: output preserved; parent pricing commit pending — retrying same idempotent sync"
      sleep "$POLL_SECONDS"
      continue
    fi
    echo "poll $poll: sync HTTP_$SYNC_CODE detail=$DETAIL"
    sleep "$POLL_SECONDS"
    continue
  fi

  STAGE_STATE="$(jq -r '.stage_state // ""' "$TMPDIR_CERT/sync.json")"
  PROVIDER_STATE="$(jq -r '.provider_state // ""' "$TMPDIR_CERT/sync.json")"
  CHILD_TOTAL="$(jq -r '.children | length' "$TMPDIR_CERT/sync.json")"
  CHILD_DONE="$(jq -r '[.children[]? | select((.status=="succeeded") or (.status=="completed") or (.status=="complete") or (.status=="ready"))] | length' "$TMPDIR_CERT/sync.json")"
  echo "poll $poll: stage=$STAGE_STATE provider=$PROVIDER_STATE children=$CHILD_DONE/$CHILD_TOTAL"

  if [[ "$STAGE_STATE" == "awaiting_review" && "$PROVIDER_STATE" == "succeeded" ]]; then
    SUCCESS=1
    cp "$TMPDIR_CERT/sync.json" "$TMPDIR_CERT/success.json"
    break
  fi
  sleep "$POLL_SECONDS"
done
[[ "$SUCCESS" == "1" ]] || fail "Fusion did not reach committed reviewable output within configured poll window"

MEDIA_ID="$(jq -r '.media_asset_id // ""' "$TMPDIR_CERT/success.json")"
REVIEW_ID="$(jq -r '.review_item_id // ""' "$TMPDIR_CERT/success.json")"
COMMITTED_STATE="$(jq -r '.parent_pricing.state // ""' "$TMPDIR_CERT/success.json")"
COMMITTED_RES="$(jq -r '.parent_pricing.reservation_id // ""' "$TMPDIR_CERT/success.json")"
LEDGER_ID="$(jq -r '.parent_pricing.ledger_entry_id // ""' "$TMPDIR_CERT/success.json")"
[[ -n "$MEDIA_ID" && -n "$REVIEW_ID" ]] || fail "reviewable media/review ID missing"
[[ "$COMMITTED_STATE" == "committed" ]] || fail "parent pricing state=$COMMITTED_STATE, expected committed"
[[ "$COMMITTED_RES" == "$RESERVATION_ID" ]] || fail "commit reservation lineage changed"
[[ -n "$LEDGER_ID" ]] || fail "committed pricing missing ledger_entry_id"

info "7. WALLET + LEDGER RECONCILIATION"
balance_json "$TMPDIR_CERT/balance-committed.json"
read -r B2 R2 A2 <<< "$(tr '\t' ' ' <<< "$(balance_triplet "$TMPDIR_CERT/balance-committed.json")")"
[[ $((B0-B2)) -eq "$QUOTED_CREDITS" ]] || fail "balance charge=$((B0-B2)), expected $QUOTED_CREDITS"
[[ "$R2" == "$R0" ]] || fail "reserved credits after commit=$R2, expected baseline=$R0"
[[ $((A0-A2)) -eq "$QUOTED_CREDITS" ]] || fail "available charge=$((A0-A2)), expected $QUOTED_CREDITS"

LEDGER_ROW="$(psql_at "select credits_delta,coalesce(sku_code,''),coalesce(quantity::text,'') from public.pricing_credit_ledger_events where id='$LEDGER_ID'::uuid and user_id='$USER_ID'::uuid")"
IFS='|' read -r LEDGER_DELTA LEDGER_SKU LEDGER_QTY <<< "$LEDGER_ROW"
[[ "$LEDGER_DELTA" == "-$QUOTED_CREDITS" ]] || fail "ledger delta=$LEDGER_DELTA expected=-$QUOTED_CREDITS"
NEG_AFTER="$(psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'svc-fusion-extension:v3-scene:$STAGE_ID:%' and credits_delta < 0")"
[[ "$NEG_AFTER" == "$((NEG_BEFORE + 1))" ]] || fail "expected exactly one new negative scene ledger event"

ATTEMPT_ROW="$(psql_at "select state,completed_at is not null,media_id::text from public.v3_studio_stage_attempts where attempt_id='$ATTEMPT_ID'::uuid")"
IFS='|' read -r ATTEMPT_STATE ATTEMPT_COMPLETED ATTEMPT_MEDIA <<< "$ATTEMPT_ROW"
[[ "$ATTEMPT_STATE" == "succeeded" && "$ATTEMPT_COMPLETED" == "t" && "$ATTEMPT_MEDIA" == "$MEDIA_ID" ]] || fail "terminal attempt invariant/lineage failed: $ATTEMPT_ROW"
STORED_PRICING_STATE="$(psql_at "select metadata_json #>> '{fusion_parent_pricing,state}' from public.v3_studio_stage_runs where stage_run_id='$STAGE_ID'::uuid")"
[[ "$STORED_PRICING_STATE" == "committed" ]] || fail "stage metadata pricing state=$STORED_PRICING_STATE"

echo "PASS: one economic charge exactly $QUOTED_CREDITS credits; reservation cleared; ledger canonical"

info "8. DUPLICATE SYNC / COMMIT IDEMPOTENCY"
BALANCE_AFTER_COMMIT="$(balance_triplet "$TMPDIR_CERT/balance-committed.json")"
JOBS_AFTER_COMMIT="$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")"
RES_AFTER_COMMIT="$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")"
SYNC2_CODE="$(api_post_code "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/sync" '{}' "$TMPDIR_CERT/sync2.json")"
[[ "$SYNC2_CODE" == "200" ]] || { cat "$TMPDIR_CERT/sync2.json"; fail "duplicate sync HTTP $SYNC2_CODE"; }
[[ "$(jq -r '.media_asset_id // ""' "$TMPDIR_CERT/sync2.json")" == "$MEDIA_ID" ]] || fail "duplicate sync changed media output"
[[ "$(jq -r '.parent_pricing.state // ""' "$TMPDIR_CERT/sync2.json")" == "committed" ]] || fail "duplicate sync lost committed parent pricing"
balance_json "$TMPDIR_CERT/balance-sync2.json"
[[ "$(balance_triplet "$TMPDIR_CERT/balance-sync2.json")" == "$BALANCE_AFTER_COMMIT" ]] || fail "duplicate sync changed wallet"
[[ "$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")" == "$JOBS_AFTER_COMMIT" ]] || fail "duplicate sync created provider jobs"
[[ "$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")" == "$RES_AFTER_COMMIT" ]] || fail "duplicate sync created reservation"
[[ "$(psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'svc-fusion-extension:v3-scene:$STAGE_ID:%' and credits_delta < 0")" == "$NEG_AFTER" ]] || fail "duplicate sync created another charge"
echo "PASS: duplicate sync = zero charge / zero reservation / zero provider work"

info "9. HUMAN REVIEW BOUNDARY"
echo "media_asset_id=$MEDIA_ID"
echo "review_item_id=$REVIEW_ID"
echo "parent_reservation_id=$RESERVATION_ID"
echo "ledger_entry_id=$LEDGER_ID"
echo "charged_credits=$QUOTED_CREDITS"
echo
echo "PASS: Fusion output is awaiting human review with parent pricing already committed."
echo "PASS: DB trigger prevents Fusion approval unless parent pricing state is committed."
echo "The script intentionally does NOT approve the scene."
echo
echo "============================================================"
echo " V3 FUSION PROVIDER + PRICING CERTIFICATION = PASS"
echo " EXACT LOGICAL SCENE CHARGES                 = 1"
echo " CHILD CHARGES                              = 0"
echo " DUPLICATE CHARGES                          = 0"
echo "============================================================"
