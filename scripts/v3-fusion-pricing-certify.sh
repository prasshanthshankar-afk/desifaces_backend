#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIRECTOR_BASE="${DIRECTOR_BASE:-http://127.0.0.1:18011}"
EXT_BASE="${EXT_BASE:-http://127.0.0.1:18006}"
PRICING_BASE="${PRICING_BASE:-http://127.0.0.1:18009}"
WORKFLOW_ID="${WORKFLOW_ID:-}"
AUTH_TOKEN="${AUTH_TOKEN:-${DF_AUTH_TOKEN:-}}"
COMPOSE=(bash "$ROOT/scripts/v3-compose.sh")

fail() {
  echo "V3 FUSION PRICING CERT: FAIL: $*" >&2
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

api_get() {
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
  local base="$1" path="$2" body="$3" out="$4"
  curl -sS -o "$out" -w '%{http_code}' \
    -H "$AUTH_HEADER" \
    -H 'Content-Type: application/json' \
    -X POST --data "$body" "$base$path"
}

psql_at() {
  "${COMPOSE[@]}" exec -T desifaces-db \
    psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
}

balance_json() {
  local out="$1"
  api_get "$PRICING_BASE" "/api/credits/balance" "$out" || fail "pricing balance lookup failed"
}

balance_triplet() {
  jq -r '[.balance_credits,.reserved_credits,.available_credits] | @tsv' "$1"
}

info "1. CANONICAL WORKFLOW GATE"
api_get "$DIRECTOR_BASE" "/api/director/studio-workflows/$WORKFLOW_ID" "$TMPDIR_CERT/workflow.json" \
  || fail "workflow lookup failed"

CURRENT_STAGE="$(jq -r '.current_stage // ""' "$TMPDIR_CERT/workflow.json")"
[[ "$CURRENT_STAGE" == "fusion" ]] || fail "workflow stage is '$CURRENT_STAGE', expected fusion"

STAGE_ID="$(jq -r '[.stages[] | select(.stage_type=="fusion" and .scope_type=="scene" and .state!="approved")][0].stage_run_id // ""' "$TMPDIR_CERT/workflow.json")"
[[ -n "$STAGE_ID" ]] || fail "Fusion stage not found"

IDENTITY="$(psql_at "select owner_user_id::text,project_id::text from public.v3_studio_workflows where workflow_id='$WORKFLOW_ID'::uuid")"
IFS='|' read -r USER_ID PROJECT_ID <<< "$IDENTITY"
[[ -n "$USER_ID" && -n "$PROJECT_ID" ]] || fail "workflow owner/project identity missing"

echo "workflow_id=$WORKFLOW_ID"
echo "stage_id=$STAGE_ID"
echo "project_id=$PROJECT_ID"

info "2. PRICING CATALOG INTEGRITY"
CATALOG="$(psql_at "select s.unit,s.default_unit_credits,coalesce(s.provider_hint,''),(select count(*) from pricing_variant_lines vl where vl.variant_code='FUSION_TALKING_VIDEO' and vl.sku_code='FUSION_TALK_MIN') from pricing_skus s where s.code='FUSION_TALK_MIN'")"
IFS='|' read -r SKU_UNIT SKU_CREDITS SKU_PROVIDER VARIANT_LINE_COUNT <<< "$CATALOG"
[[ "$SKU_UNIT" == "minute" ]] || fail "FUSION_TALK_MIN unit=$SKU_UNIT, expected minute"
[[ "$SKU_CREDITS" =~ ^[0-9]+$ && "$SKU_CREDITS" -gt 0 ]] || fail "invalid Fusion DB credit rate: $SKU_CREDITS"
[[ -z "$SKU_PROVIDER" ]] || fail "pricing SKU contains provider-specific metadata: $SKU_PROVIDER"
[[ "$VARIANT_LINE_COUNT" == "1" ]] || fail "FUSION_TALKING_VIDEO must contain exactly one FUSION_TALK_MIN line"

PREMIUM_ACTIVE="$(psql_at "select is_active::text from pricing_experience_packages where package_code='V3_MULTIPERSON_PREMIUM'")"
[[ "$PREMIUM_ACTIVE" == "false" ]] || fail "V3 multi-person premium package must remain dormant"

echo "PASS: DB owns Fusion price: $SKU_CREDITS credits/minute; provider-neutral; premium dormant"

info "3. BASELINE WALLET + ECONOMIC COUNTS"
balance_json "$TMPDIR_CERT/balance-before.json"
BALANCE_BEFORE="$(balance_triplet "$TMPDIR_CERT/balance-before.json")"
RES_BEFORE="$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")"
CHARGE_BEFORE="$(psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'svc-fusion-extension:v3-scene:$STAGE_ID:%' and credits_delta < 0")"
JOBS_BEFORE="$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")"

echo "balance/reserved/available=$BALANCE_BEFORE"
echo "scene_reservations=$RES_BEFORE"
echo "scene_negative_ledger_events=$CHARGE_BEFORE"
echo "fusion_provider_jobs=$JOBS_BEFORE"

info "4. PREVIEW #1 — ONE PARENT / ZERO BILLABLE CHILDREN"
PREVIEW_BODY='{"external_provider_ok":true}'
CODE="$(api_post_code "$DIRECTOR_BASE" "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/pricing-preview" "$PREVIEW_BODY" "$TMPDIR_CERT/preview1.json")"
[[ "$CODE" == "200" ]] || { cat "$TMPDIR_CERT/preview1.json"; fail "preview #1 HTTP $CODE"; }

TURN_COUNT="$(jq -r '.turn_count // 0' "$TMPDIR_CERT/preview1.json")"
PARENT_COUNT="$(jq -r '.billable_parent_quote_count // 0' "$TMPDIR_CERT/preview1.json")"
CHILD_BILLABLE="$(jq -r '.billable_child_quote_count // -1' "$TMPDIR_CERT/preview1.json")"
REQUIRED_CHILD="$(jq -r '.required_child_count // 0' "$TMPDIR_CERT/preview1.json")"
SUPPRESSED_CHILD="$(jq -r '.child_pricing_suppressed // 0' "$TMPDIR_CERT/preview1.json")"

[[ "$TURN_COUNT" -gt 0 ]] || fail "preview has no turns"
[[ "$PARENT_COUNT" == "1" ]] || fail "expected exactly one billable parent quote"
[[ "$CHILD_BILLABLE" == "0" ]] || fail "billable child quote count is $CHILD_BILLABLE"
[[ "$SUPPRESSED_CHILD" == "$REQUIRED_CHILD" ]] || fail "child suppression mismatch: $SUPPRESSED_CHILD/$REQUIRED_CHILD"

jq -e 'all(.children[]?; .pricing_suppressed == true and ((.pricing.state // "") == "suppressed") and ((.pricing.quote_id // "") == "") and ((.pricing.reservation_id // "") == "") and ((.pricing.billed_units // "0") == "0"))' "$TMPDIR_CERT/preview1.json" >/dev/null \
  || fail "one or more child pricing contracts are not zero/suppressed"

PARENT_UNIT="$(jq -r '.parent_quote.pricing.unit_type // ""' "$TMPDIR_CERT/preview1.json")"
PARENT_PROVIDER="$(jq -r '.parent_quote.provider // .parent_quote.pricing.provider // .parent_quote.pricing.meta.provider // ""' "$TMPDIR_CERT/preview1.json")"
DURATION_SOURCE="$(jq -r '.parent_quote.duration_source // ""' "$TMPDIR_CERT/preview1.json")"
TOTAL_SECONDS="$(jq -r '.parent_quote.total_audio_duration_sec // 0' "$TMPDIR_CERT/preview1.json")"
MINUTES="$(jq -r '.parent_quote.billable_minutes // 0' "$TMPDIR_CERT/preview1.json")"
QUOTE_ID="$(jq -r '.parent_quote.pricing.quote_id // ""' "$TMPDIR_CERT/preview1.json")"
FINGERPRINT="$(jq -r '.parent_quote.pricing.preview_fingerprint // ""' "$TMPDIR_CERT/preview1.json")"
QUOTED_CREDITS="$(jq -r '.parent_quote.pricing.meta.total_credits // 0' "$TMPDIR_CERT/preview1.json")"

[[ "$PARENT_UNIT" == "minute" ]] || fail "parent pricing unit=$PARENT_UNIT, expected minute"
[[ "$PARENT_PROVIDER" == "veed_fabric" ]] || fail "parent execution provider=$PARENT_PROVIDER, expected veed_fabric"
[[ "$DURATION_SOURCE" == "approved_audio_ffprobe" ]] || fail "duration source=$DURATION_SOURCE, expected approved_audio_ffprobe"
[[ -n "$QUOTE_ID" && -n "$FINGERPRINT" ]] || fail "parent quote confirmation contract missing"
[[ "$QUOTED_CREDITS" =~ ^[0-9]+$ && "$QUOTED_CREDITS" -gt 0 ]] || fail "quoted credits missing/invalid: $QUOTED_CREDITS"

EXPECTED_MINUTES="$(python3 - "$TOTAL_SECONDS" <<'PY'
import math, sys
value=float(sys.argv[1])
print(max(1, math.ceil(max(value, 0.001)/60.0)))
PY
)"
[[ "$MINUTES" == "$EXPECTED_MINUTES" ]] || fail "billable minutes=$MINUTES expected=$EXPECTED_MINUTES from actual audio duration=$TOTAL_SECONDS"
EXPECTED_CREDITS=$((MINUTES * SKU_CREDITS))
[[ "$QUOTED_CREDITS" == "$EXPECTED_CREDITS" ]] || fail "quote credits=$QUOTED_CREDITS expected=$EXPECTED_CREDITS ($MINUTES*$SKU_CREDITS)"

jq -e '(.parent_quote.pricing.meta.lines | length) == 1 and .parent_quote.pricing.meta.lines[0].unit == "minute" and ((.parent_quote.pricing.meta.lines[0].provider_hint // "") == "")' "$TMPDIR_CERT/preview1.json" >/dev/null \
  || fail "parent pricebook line is not one provider-neutral minute line"
if grep -qi 'heygen' "$TMPDIR_CERT/preview1.json"; then
  fail "stale heygen metadata leaked into parent pricing contract"
fi

echo "PASS: actual_audio_seconds=$TOTAL_SECONDS billable_minutes=$MINUTES quoted_credits=$QUOTED_CREDITS"
echo "PASS: one parent quote; $SUPPRESSED_CHILD/$REQUIRED_CHILD required child renders pricing-suppressed"

info "5. PREVIEW #2 — DETERMINISTIC + ZERO WALLET MUTATION"
CODE="$(api_post_code "$DIRECTOR_BASE" "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/pricing-preview" "$PREVIEW_BODY" "$TMPDIR_CERT/preview2.json")"
[[ "$CODE" == "200" ]] || { cat "$TMPDIR_CERT/preview2.json"; fail "preview #2 HTTP $CODE"; }

for path in \
  '.parent_quote.pricing.quote_id' \
  '.parent_quote.pricing.preview_fingerprint' \
  '.parent_quote.pricing.meta.total_credits' \
  '.parent_quote.billable_minutes' \
  '.parent_quote.audio_lineage_hash'; do
  A="$(jq -r "$path" "$TMPDIR_CERT/preview1.json")"
  B="$(jq -r "$path" "$TMPDIR_CERT/preview2.json")"
  [[ "$A" == "$B" ]] || fail "preview determinism mismatch for $path: $A != $B"
done

balance_json "$TMPDIR_CERT/balance-after-previews.json"
BALANCE_AFTER_PREVIEWS="$(balance_triplet "$TMPDIR_CERT/balance-after-previews.json")"
RES_AFTER_PREVIEWS="$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")"
CHARGE_AFTER_PREVIEWS="$(psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'svc-fusion-extension:v3-scene:$STAGE_ID:%' and credits_delta < 0")"
JOBS_AFTER_PREVIEWS="$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")"

[[ "$BALANCE_AFTER_PREVIEWS" == "$BALANCE_BEFORE" ]] || fail "pricing preview mutated wallet: before=$BALANCE_BEFORE after=$BALANCE_AFTER_PREVIEWS"
[[ "$RES_AFTER_PREVIEWS" == "$RES_BEFORE" ]] || fail "pricing preview created a reservation"
[[ "$CHARGE_AFTER_PREVIEWS" == "$CHARGE_BEFORE" ]] || fail "pricing preview created a negative ledger event"
[[ "$JOBS_AFTER_PREVIEWS" == "$JOBS_BEFORE" ]] || fail "pricing preview created a Fusion provider job"

echo "PASS: repeat preview is deterministic and non-mutating"

info "6. INVALID CONFIRMATION — FAIL CLOSED BEFORE RESERVE/PROVIDER"
CHILD_CONFIRMATIONS="$(jq -c '[.children[] | {dialogue_turn_id,request_nonce}]' "$TMPDIR_CERT/preview2.json")"
BAD_BODY="$(jq -cn --argjson children "$CHILD_CONFIRMATIONS" --arg fp "$FINGERPRINT" '{parent_confirmation:{quote_id:"qt_intentionally_wrong",preview_fingerprint:$fp},child_confirmations:$children,external_provider_ok:true,user_confirmed:true}')"
BAD_CODE="$(api_post_code "$DIRECTOR_BASE" "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/dispatch" "$BAD_BODY" "$TMPDIR_CERT/bad-dispatch.json")"
[[ "$BAD_CODE" == "409" ]] || { cat "$TMPDIR_CERT/bad-dispatch.json"; fail "bad pricing confirmation must return 409, got $BAD_CODE"; }

balance_json "$TMPDIR_CERT/balance-after-bad.json"
[[ "$(balance_triplet "$TMPDIR_CERT/balance-after-bad.json")" == "$BALANCE_BEFORE" ]] || fail "bad confirmation mutated wallet"
[[ "$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")" == "$RES_BEFORE" ]] || fail "bad confirmation created reservation"
[[ "$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")" == "$JOBS_BEFORE" ]] || fail "bad confirmation dispatched provider job"

echo "PASS: mismatched quote rejected before reservation and before provider dispatch"

info "7. REAL RESERVE IDEMPOTENCY — NO PROVIDER DISPATCH"
QUOTE_ID="$(jq -r '.parent_quote.pricing.quote_id' "$TMPDIR_CERT/preview2.json")"
FINGERPRINT="$(jq -r '.parent_quote.pricing.preview_fingerprint' "$TMPDIR_CERT/preview2.json")"
RESERVE_BODY="$(jq -cn --arg project "$PROJECT_ID" --arg workflow "$WORKFLOW_ID" --arg stage "$STAGE_ID" --arg quote "$QUOTE_ID" --arg fp "$FINGERPRINT" '{project_id:$project,workflow_id:$workflow,stage_run_id:$stage,quote_id:$quote,preview_fingerprint:$fp}')"

RES_CODE="$(api_post_code "$EXT_BASE" "/api/longform/v3/scene-pricing/reserve" "$RESERVE_BODY" "$TMPDIR_CERT/reserve1.json")"
[[ "$RES_CODE" == "200" ]] || { cat "$TMPDIR_CERT/reserve1.json"; fail "parent reserve HTTP $RES_CODE"; }
RESERVATION_ID="$(jq -r '.pricing.reservation_id // ""' "$TMPDIR_CERT/reserve1.json")"
[[ -n "$RESERVATION_ID" ]] || fail "reserve response missing reservation_id"
[[ "$(jq -r '.pricing.state // ""' "$TMPDIR_CERT/reserve1.json")" == "reserved" ]] || fail "parent pricing did not enter reserved state"

balance_json "$TMPDIR_CERT/balance-reserved1.json"
read -r B0 R0 A0 <<< "$(tr '\t' ' ' <<< "$BALANCE_BEFORE")"
read -r B1 R1 A1 <<< "$(tr '\t' ' ' <<< "$(balance_triplet "$TMPDIR_CERT/balance-reserved1.json")")"
[[ "$B1" == "$B0" ]] || fail "reserve changed balance_credits: $B0 -> $B1"
[[ $((R1 - R0)) -eq "$QUOTED_CREDITS" ]] || fail "reserved_credits delta=$((R1-R0)), expected $QUOTED_CREDITS"
[[ $((A0 - A1)) -eq "$QUOTED_CREDITS" ]] || fail "available_credits delta=$((A0-A1)), expected $QUOTED_CREDITS"
[[ "$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")" == "$((RES_BEFORE + 1))" ]] || fail "reserve did not create exactly one scene reservation"
[[ "$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")" == "$JOBS_BEFORE" ]] || fail "reserve-only test dispatched a Fusion job"

echo "PASS: one parent reservation holds exactly $QUOTED_CREDITS credits"

RES2_CODE="$(api_post_code "$EXT_BASE" "/api/longform/v3/scene-pricing/reserve" "$RESERVE_BODY" "$TMPDIR_CERT/reserve2.json")"
[[ "$RES2_CODE" == "200" ]] || fail "duplicate reserve HTTP $RES2_CODE"
[[ "$(jq -r '.pricing.reservation_id // ""' "$TMPDIR_CERT/reserve2.json")" == "$RESERVATION_ID" ]] || fail "duplicate reserve returned a different reservation"
balance_json "$TMPDIR_CERT/balance-reserved2.json"
[[ "$(balance_triplet "$TMPDIR_CERT/balance-reserved2.json")" == "$(balance_triplet "$TMPDIR_CERT/balance-reserved1.json")" ]] || fail "duplicate reserve changed wallet"
[[ "$(psql_at "select count(*) from public.pricing_credit_reservations where user_id='$USER_ID'::uuid and job_ref='$STAGE_ID'")" == "$((RES_BEFORE + 1))" ]] || fail "duplicate reserve created another reservation"
echo "PASS: duplicate reserve is economically idempotent"

info "8. RELEASE + DUPLICATE RELEASE — EXACT WALLET RESTORATION"
RELEASE_BODY="$(jq -cn --arg project "$PROJECT_ID" --arg workflow "$WORKFLOW_ID" --arg stage "$STAGE_ID" '{project_id:$project,workflow_id:$workflow,stage_run_id:$stage,reason:"pricing_certification_release"}')"
REL_CODE="$(api_post_code "$EXT_BASE" "/api/longform/v3/scene-pricing/release" "$RELEASE_BODY" "$TMPDIR_CERT/release1.json")"
[[ "$REL_CODE" == "200" ]] || { cat "$TMPDIR_CERT/release1.json"; fail "release HTTP $REL_CODE"; }
[[ "$(jq -r '.pricing.state // ""' "$TMPDIR_CERT/release1.json")" == "released" ]] || fail "pricing did not enter released state"

balance_json "$TMPDIR_CERT/balance-released1.json"
[[ "$(balance_triplet "$TMPDIR_CERT/balance-released1.json")" == "$BALANCE_BEFORE" ]] || fail "release did not restore exact wallet baseline"
[[ "$(psql_at "select count(*) from public.pricing_credit_ledger_events where user_id='$USER_ID'::uuid and idempotency_key like 'svc-fusion-extension:v3-scene:$STAGE_ID:%' and credits_delta < 0")" == "$CHARGE_BEFORE" ]] || fail "reserve/release created a consumption ledger event"

REL2_CODE="$(api_post_code "$EXT_BASE" "/api/longform/v3/scene-pricing/release" "$RELEASE_BODY" "$TMPDIR_CERT/release2.json")"
[[ "$REL2_CODE" == "200" ]] || fail "duplicate release HTTP $REL2_CODE"
balance_json "$TMPDIR_CERT/balance-released2.json"
[[ "$(balance_triplet "$TMPDIR_CERT/balance-released2.json")" == "$BALANCE_BEFORE" ]] || fail "duplicate release changed wallet"
[[ "$(psql_at "select count(*) from public.studio_jobs where user_id='$USER_ID'::uuid and studio_type='fusion'")" == "$JOBS_BEFORE" ]] || fail "reserve/release certification dispatched provider work"
echo "PASS: release restores wallet exactly; duplicate release is idempotent; no provider work"

info "9. FRESH FINAL PREVIEW AFTER RELEASE"
FINAL_CODE="$(api_post_code "$DIRECTOR_BASE" "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/pricing-preview" "$PREVIEW_BODY" "$TMPDIR_CERT/final-preview.json")"
[[ "$FINAL_CODE" == "200" ]] || { cat "$TMPDIR_CERT/final-preview.json"; fail "final preview HTTP $FINAL_CODE"; }

jq -e --argjson turns "$TURN_COUNT" '
  .billable_parent_quote_count == 1
  and .billable_child_quote_count == 0
  and .turn_count == $turns
  and .child_pricing_suppressed == .required_child_count
  and .parent_quote.pricing.unit_type == "minute"
  and .parent_quote.provider == "veed_fabric"
  and ((.parent_quote.pricing.quote_id // "") | length) > 0
  and ((.parent_quote.pricing.preview_fingerprint // "") | length) > 0
' "$TMPDIR_CERT/final-preview.json" >/dev/null || fail "final preview safety contract failed"

balance_json "$TMPDIR_CERT/balance-final.json"
[[ "$(balance_triplet "$TMPDIR_CERT/balance-final.json")" == "$BALANCE_BEFORE" ]] || fail "final preview mutated wallet"

FINAL_QUOTE="$(jq -r '.parent_quote.pricing.quote_id' "$TMPDIR_CERT/final-preview.json")"
FINAL_CREDITS="$(jq -r '.parent_quote.pricing.meta.total_credits' "$TMPDIR_CERT/final-preview.json")"
FINAL_MINUTES="$(jq -r '.parent_quote.billable_minutes' "$TMPDIR_CERT/final-preview.json")"
FINAL_SECONDS="$(jq -r '.parent_quote.total_audio_duration_sec' "$TMPDIR_CERT/final-preview.json")"

info "10. CERTIFICATION RESULT"
echo "PASS: PRICING CATALOG                = minute / provider-neutral / DB-owned"
echo "PASS: PARENT QUOTES                  = 1"
echo "PASS: BILLABLE CHILD QUOTES          = 0"
echo "PASS: CHILD PRICING SUPPRESSION      = $SUPPRESSED_CHILD/$REQUIRED_CHILD"
echo "PASS: DURATION SOURCE                = approved Audio + ffprobe"
echo "PASS: ACTUAL AUDIO DURATION          = $FINAL_SECONDS sec"
echo "PASS: BILLABLE LOGICAL MINUTES       = $FINAL_MINUTES"
echo "PASS: CURRENT QUOTED CREDITS         = $FINAL_CREDITS"
echo "PASS: REPEAT PREVIEW                 = deterministic / zero wallet mutation"
echo "PASS: BAD CONFIRMATION               = fail closed before reserve/provider"
echo "PASS: RESERVE                        = exactly one hold"
echo "PASS: DUPLICATE RESERVE              = zero additional hold"
echo "PASS: RELEASE                        = exact wallet restoration"
echo "PASS: DUPLICATE RELEASE              = zero wallet mutation"
echo "PASS: PROVIDER JOBS DURING CERT      = 0"
echo "PASS: NEGATIVE LEDGER CHARGE DELTA   = 0"
echo "PASS: FUSION GENERATION              = NOT STARTED"
echo
echo "final_quote_id=$FINAL_QUOTE"
echo
echo "============================================================"
echo " V3 FUSION PRICING CERTIFICATION = PASS"
echo " BILLABLE GENERATION GATE        = ELIGIBLE"
echo "============================================================"
