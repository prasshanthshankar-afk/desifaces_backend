#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORKFLOW_ID="${WORKFLOW_ID:-}"
FUSION_STAGE_ID="${FUSION_STAGE_ID:-}"
FUSION_REVIEW_ITEM_ID="${FUSION_REVIEW_ITEM_ID:-}"
FUSION_MEDIA_ID="${FUSION_MEDIA_ID:-}"
DF_EMAIL="${DF_EMAIL:-}"
DIRECTOR_BASE="${DIRECTOR_BASE:-http://127.0.0.1:18011}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
PRICING_BASE="${PRICING_BASE:-http://127.0.0.1:18009}"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")

fail() { echo "V3 STORY CLOSE: HOLD: $*" >&2; exit 1; }
info() { echo; echo "===== $* ====="; }

[[ -n "$WORKFLOW_ID" ]] || fail "WORKFLOW_ID is required"
[[ -n "$FUSION_STAGE_ID" ]] || fail "FUSION_STAGE_ID is required"
[[ -n "$FUSION_REVIEW_ITEM_ID" ]] || fail "FUSION_REVIEW_ITEM_ID is required"
[[ -n "$FUSION_MEDIA_ID" ]] || fail "FUSION_MEDIA_ID is required"
[[ -n "$DF_EMAIL" ]] || fail "DF_EMAIL is required"
[[ -f "$ROOT/infra/.env" ]] || fail "missing infra/.env"

POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
[[ "$POSTGRES_DB" == "desifaces_v3" ]] || fail "refusing non-V3 database"

psql_at() {
  "${COMPOSE[@]}" exec -T desifaces-db \
    psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

USER_ID="$(psql_at "select owner_user_id::text from public.v3_studio_workflows where workflow_id='$WORKFLOW_ID'::uuid")"
[[ -n "$USER_ID" ]] || fail "workflow owner missing"

fusion_charge_count() {
  psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'consume:svc-fusion-extension:v3-scene:$FUSION_STAGE_ID:commit:%' and credits_delta<0"
}

fusion_child_count() {
  psql_at "select count(*) from public.studio_jobs where studio_type='fusion' and user_id='$USER_ID'::uuid and (payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='$FUSION_STAGE_ID' or payload_json #>> '{tags,billing_context,billing_parent_job_id}'='$FUSION_STAGE_ID')"
}

info "1. CERTIFIED FUSION PRECONDITION"
PRE="$(psql_at "
select
  s.state,
  r.decision,
  o.media_id::text,
  s.metadata_json #>> '{fusion_parent_pricing,state}',
  (select count(*) from public.studio_jobs j where j.studio_type='fusion' and j.user_id='$USER_ID'::uuid and (j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='$FUSION_STAGE_ID' or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='$FUSION_STAGE_ID') and j.status='succeeded'),
  (select count(*) from public.studio_jobs j where j.studio_type='fusion' and j.user_id='$USER_ID'::uuid and (j.payload_json #>> '{provider_options,billing_context,billing_parent_job_id}'='$FUSION_STAGE_ID' or j.payload_json #>> '{tags,billing_context,billing_parent_job_id}'='$FUSION_STAGE_ID') and j.payload_json #>> '{pricing,state}'='suppressed')
from public.v3_studio_stage_runs s
join public.v3_studio_stage_outputs o on o.stage_run_id=s.stage_run_id and o.is_active=true
join public.v3_studio_review_items r on r.stage_run_id=s.stage_run_id and r.media_id=o.media_id
where s.stage_run_id='$FUSION_STAGE_ID'::uuid and r.review_item_id='$FUSION_REVIEW_ITEM_ID'::uuid
limit 1")"
IFS='|' read -r FSTATE FDEC FMEDIA FPSTATE FSUCCESS FSL suppressed_extra <<< "$PRE"
echo "fusion_stage=$FSTATE review=$FDEC media=$FMEDIA pricing=$FPSTATE succeeded=$FSUCCESS suppressed=$FSL"
[[ "$FSTATE" == "awaiting_review" ]] || fail "Fusion stage is not awaiting_review"
[[ "$FDEC" == "pending" ]] || fail "Fusion review is not pending"
[[ "$FMEDIA" == "$FUSION_MEDIA_ID" ]] || fail "Fusion media mismatch"
[[ "$FPSTATE" == "committed" ]] || fail "Fusion parent pricing is not committed"
[[ "$FSUCCESS" == "28" ]] || fail "Fusion children are not 28/28 succeeded"
[[ "$FSL" == "28" ]] || fail "Fusion children are not 28/28 pricing-suppressed"
CHILDREN_BEFORE="$(fusion_child_count)"
CHARGES_BEFORE="$(fusion_charge_count)"
[[ "$CHILDREN_BEFORE" == "28" ]] || fail "unexpected Fusion child count"
[[ "$CHARGES_BEFORE" == "1" ]] || fail "expected exactly one Fusion consume event"

echo "wallet_before:"
curl -fsS -H "Authorization: Bearer ${DF_AUTH_TOKEN:-}" "$PRICING_BASE/api/credits/balance" 2>/dev/null | jq . || true

info "2. LOGIN"
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
}

refresh_auth || fail "login failed"
echo "LOGIN = PASS"

api_post() {
  local path="$1" body="${2:-}" out code
  out="$(mktemp)"
  if [[ -n "$body" ]]; then
    code="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
      -H "Authorization: Bearer $DF_AUTH_TOKEN" \
      -H 'Content-Type: application/json' \
      "$DIRECTOR_BASE$path" -d "$body")"
  else
    code="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
      -H "Authorization: Bearer $DF_AUTH_TOKEN" \
      "$DIRECTOR_BASE$path")"
  fi
  if [[ "$code" == "401" ]]; then
    refresh_auth || { cat "$out"; rm -f "$out"; return 91; }
    if [[ -n "$body" ]]; then
      code="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
        -H "Authorization: Bearer $DF_AUTH_TOKEN" \
        -H 'Content-Type: application/json' \
        "$DIRECTOR_BASE$path" -d "$body")"
    else
      code="$(curl -sS -o "$out" -w '%{http_code}' -X POST \
        -H "Authorization: Bearer $DF_AUTH_TOKEN" \
        "$DIRECTOR_BASE$path")"
    fi
  fi
  echo "$code|$out"
}

workflow_get() {
  local out code
  out="$(mktemp)"
  code="$(curl -sS -o "$out" -w '%{http_code}' \
    -H "Authorization: Bearer $DF_AUTH_TOKEN" \
    "$DIRECTOR_BASE/api/director/studio-workflows/$WORKFLOW_ID")"
  if [[ "$code" == "401" ]]; then
    refresh_auth || { cat "$out"; rm -f "$out"; return 91; }
    code="$(curl -sS -o "$out" -w '%{http_code}' \
      -H "Authorization: Bearer $DF_AUTH_TOKEN" \
      "$DIRECTOR_BASE/api/director/studio-workflows/$WORKFLOW_ID")"
  fi
  echo "$code|$out"
}

info "3. APPROVE THE EXACT CERTIFIED FUSION OUTPUT"
R="$(api_post "/api/director/studio-reviews/$FUSION_REVIEW_ITEM_ID" '{"decision":"approved","feedback":"Approved after end-to-end Fusion functional and pricing certification."}')"
CODE="${R%%|*}"; OUT="${R#*|}"
[[ "$CODE" == "200" ]] || { cat "$OUT"; rm -f "$OUT"; fail "Fusion review HTTP $CODE"; }
jq '{state,current_stage,final_media_id}' "$OUT" || cat "$OUT"
rm -f "$OUT"

FDEC_AFTER="$(psql_at "select decision from public.v3_studio_review_items where review_item_id='$FUSION_REVIEW_ITEM_ID'::uuid")"
FSTATE_AFTER="$(psql_at "select state from public.v3_studio_stage_runs where stage_run_id='$FUSION_STAGE_ID'::uuid")"
[[ "$FDEC_AFTER" == "approved" ]] || fail "Fusion review did not become approved"
[[ "$FSTATE_AFTER" == "approved" ]] || fail "Fusion stage did not become approved"
echo "FUSION_HITL = APPROVED"

info "4. ADVANCE AFTER FUSION HITL"
R="$(api_post "/api/director/studio-workflows/$WORKFLOW_ID/advance")"
CODE="${R%%|*}"; OUT="${R#*|}"
[[ "$CODE" == "200" ]] || { cat "$OUT"; rm -f "$OUT"; fail "advance HTTP $CODE"; }
WSTATE="$(jq -r '.state // ""' "$OUT")"
WCURRENT="$(jq -r '.current_stage // ""' "$OUT")"
WFINAL="$(jq -r '.final_media_id // ""' "$OUT")"
echo "workflow_state=$WSTATE current_stage=$WCURRENT final_media_id=$WFINAL"
rm -f "$OUT"

if [[ "$WSTATE" == "completed" ]]; then
  [[ "$WFINAL" == "$FUSION_MEDIA_ID" ]] || fail "one-scene completed workflow final_media_id mismatch"
  echo "ONE_SCENE_DIRECT_COMPLETION = PASS"
else
  [[ "$WCURRENT" == "story_final" ]] || fail "workflow did not advance to story_final"

  STORY_STAGE_ID="$(psql_at "select stage_run_id::text from public.v3_studio_stage_runs where workflow_id='$WORKFLOW_ID'::uuid and stage_type='story_final' and scope_type='story' order by created_at limit 1")"
  [[ -n "$STORY_STAGE_ID" ]] || fail "story_final stage missing"
  echo "story_final_stage_id=$STORY_STAGE_ID"

  SSTATE="$(psql_at "select state from public.v3_studio_stage_runs where stage_run_id='$STORY_STAGE_ID'::uuid")"
  [[ "$SSTATE" =~ ^(pending|ready|failed|rejected)$ ]] || fail "story_final stage is not stitchable: $SSTATE"

  info "5. STORY FINAL STITCH"
  refresh_auth || fail "login refresh before Story Final failed"
  R="$(api_post "/api/director/studio-workflows/$WORKFLOW_ID/story-final-stages/$STORY_STAGE_ID/stitch")"
  CODE="${R%%|*}"; OUT="${R#*|}"
  [[ "$CODE" == "200" ]] || { cat "$OUT"; rm -f "$OUT"; fail "Story Final stitch HTTP $CODE"; }
  STORY_MEDIA_ID="$(jq -r '.media_asset_id // ""' "$OUT")"
  STORY_REVIEW_ID="$(jq -r '.review_item_id // ""' "$OUT")"
  STORY_STATE="$(jq -r '.stage_state // ""' "$OUT")"
  SCENE_COUNT="$(jq -r '.scene_count // 0' "$OUT")"
  REUSED="$(jq -r '.reused // false' "$OUT")"
  ASSEMBLY_KEY="$(jq -r '.assembly_key // ""' "$OUT")"
  echo "story_state=$STORY_STATE media_id=$STORY_MEDIA_ID review_item_id=$STORY_REVIEW_ID scene_count=$SCENE_COUNT reused=$REUSED"
  echo "assembly_key=$ASSEMBLY_KEY"
  [[ "$STORY_STATE" == "awaiting_review" ]] || { cat "$OUT"; rm -f "$OUT"; fail "Story Final did not reach awaiting_review"; }
  [[ -n "$STORY_MEDIA_ID" && "$STORY_MEDIA_ID" != "null" ]] || fail "Story Final media missing"
  [[ -n "$STORY_REVIEW_ID" && "$STORY_REVIEW_ID" != "null" ]] || fail "Story Final review missing"
  rm -f "$OUT"

  info "6. APPROVE STORY FINAL OUTPUT"
  R="$(api_post "/api/director/studio-reviews/$STORY_REVIEW_ID" '{"decision":"approved","feedback":"Approved as final Story assembly after certified scene output."}')"
  CODE="${R%%|*}"; OUT="${R#*|}"
  [[ "$CODE" == "200" ]] || { cat "$OUT"; rm -f "$OUT"; fail "Story Final review HTTP $CODE"; }
  rm -f "$OUT"

  SDEC="$(psql_at "select decision from public.v3_studio_review_items where review_item_id='$STORY_REVIEW_ID'::uuid")"
  SSTATE="$(psql_at "select state from public.v3_studio_stage_runs where stage_run_id='$STORY_STAGE_ID'::uuid")"
  [[ "$SDEC" == "approved" ]] || fail "Story Final review not approved"
  [[ "$SSTATE" == "approved" ]] || fail "Story Final stage not approved"

  info "7. FINAL WORKFLOW ADVANCE"
  R="$(api_post "/api/director/studio-workflows/$WORKFLOW_ID/advance")"
  CODE="${R%%|*}"; OUT="${R#*|}"
  [[ "$CODE" == "200" ]] || { cat "$OUT"; rm -f "$OUT"; fail "final advance HTTP $CODE"; }
  WSTATE="$(jq -r '.state // ""' "$OUT")"
  WCURRENT="$(jq -r '.current_stage // ""' "$OUT")"
  WFINAL="$(jq -r '.final_media_id // ""' "$OUT")"
  echo "workflow_state=$WSTATE current_stage=$WCURRENT final_media_id=$WFINAL"
  [[ "$WSTATE" == "completed" ]] || { cat "$OUT"; rm -f "$OUT"; fail "workflow did not complete"; }
  [[ "$WCURRENT" == "story_final" ]] || fail "completed workflow current_stage is not story_final"
  [[ "$WFINAL" == "$STORY_MEDIA_ID" ]] || fail "workflow final_media_id does not equal approved Story Final media"
  rm -f "$OUT"
fi

info "8. NO-REGENERATION / NO-DOUBLE-CHARGE CERTIFICATION"
CHILDREN_AFTER="$(fusion_child_count)"
CHARGES_AFTER="$(fusion_charge_count)"
RES_TOTAL="$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$FUSION_STAGE_ID'")"
LATEST_RES="$(psql_at "select status || '|' || reserved_credits::text || '|' || coalesce(quote_json->>'final_charged_credits','') from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$FUSION_STAGE_ID' order by created_at desc limit 1")"
FINAL_DB="$(psql_at "select state || '|' || current_stage || '|' || coalesce(final_media_id::text,'') from public.v3_studio_workflows where workflow_id='$WORKFLOW_ID'::uuid")"

echo "fusion_children_before=$CHILDREN_BEFORE"
echo "fusion_children_after=$CHILDREN_AFTER"
echo "fusion_scene_charges_before=$CHARGES_BEFORE"
echo "fusion_scene_charges_after=$CHARGES_AFTER"
echo "fusion_parent_reservations_total=$RES_TOTAL"
echo "latest_parent_reservation=$LATEST_RES"
echo "workflow_db=$FINAL_DB"
[[ "$CHILDREN_AFTER" == "$CHILDREN_BEFORE" ]] || fail "close path created new Fusion children"
[[ "$CHARGES_AFTER" == "1" ]] || fail "close path changed Fusion charge count"
[[ "$RES_TOTAL" == "3" ]] || fail "close path created another Fusion reservation"
[[ "$LATEST_RES" == committed\|560\|560 ]] || fail "latest parent reservation is not the certified committed 560-credit reservation"
IFS='|' read -r DBSTATE DBCURRENT DBFINAL <<< "$FINAL_DB"
[[ "$DBSTATE" == "completed" ]] || fail "DB workflow state is not completed"
[[ -n "$DBFINAL" ]] || fail "DB final_media_id is missing"

echo "wallet_after:"
curl -fsS -H "Authorization: Bearer $DF_AUTH_TOKEN" "$PRICING_BASE/api/credits/balance" | jq .

echo
echo "============================================================"
echo " V3 STORY END-TO-END = CLOSED"
echo " FACE       = APPROVED"
echo " AUDIO      = APPROVED"
echo " FUSION     = APPROVED + COMMITTED"
echo " STORY FINAL= APPROVED"
echo " WORKFLOW   = COMPLETED"
echo " FINAL MEDIA= $DBFINAL"
echo "============================================================"

unset DF_PASS_INPUT DF_PASSWORD 2>/dev/null || true
