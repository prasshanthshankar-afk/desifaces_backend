#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIRECTOR_BASE="${DIRECTOR_BASE:-http://127.0.0.1:18011}"
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

AUTH_HEADER="Authorization: Bearer $AUTH_TOKEN"
TMPDIR_CERT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CERT"' EXIT

api_get() {
  local path="$1"
  curl -fsS -H "$AUTH_HEADER" "$DIRECTOR_BASE$path"
}

api_post() {
  local path="$1"
  local body="$2"
  curl -fsS \
    -H "$AUTH_HEADER" \
    -H 'Content-Type: application/json' \
    -X POST \
    --data "$body" \
    "$DIRECTOR_BASE$path"
}

info "1. CANONICAL WORKFLOW + PRODUCTION PREFLIGHT"
WORKFLOW_JSON="$(api_get "/api/director/studio-workflows/$WORKFLOW_ID")" || fail "workflow lookup failed"
printf '%s' "$WORKFLOW_JSON" > "$TMPDIR_CERT/workflow.json"

STATE="$(jq -r '.state // ""' "$TMPDIR_CERT/workflow.json")"
CURRENT_STAGE="$(jq -r '.current_stage // ""' "$TMPDIR_CERT/workflow.json")"
FINAL_MEDIA="$(jq -r '.final_media_id // ""' "$TMPDIR_CERT/workflow.json")"

if [[ "$STATE" == "completed" ]]; then
  [[ -n "$FINAL_MEDIA" && "$FINAL_MEDIA" != "null" ]] || fail "workflow is completed but final_media_id is empty"
  echo "PASS: workflow already completed with final_media_id=$FINAL_MEDIA"
  exit 0
fi

[[ "$CURRENT_STAGE" == "fusion" ]] || fail "workflow current_stage is '$CURRENT_STAGE', expected 'fusion'"

PREFLIGHT_JSON="$(api_get "/api/director/studio-workflows/$WORKFLOW_ID/preflight")" || fail "preflight failed"
printf '%s' "$PREFLIGHT_JSON" > "$TMPDIR_CERT/preflight.json"

FACE_APPROVED="$(jq -r '.face.approved // 0' "$TMPDIR_CERT/preflight.json")"
FACE_TOTAL="$(jq -r '.face.total // 0' "$TMPDIR_CERT/preflight.json")"
AUDIO_APPROVED="$(jq -r '.audio.approved // 0' "$TMPDIR_CERT/preflight.json")"
AUDIO_TOTAL="$(jq -r '.audio.total // 0' "$TMPDIR_CERT/preflight.json")"
FUSION_TOTAL="$(jq -r '.fusion.total // 0' "$TMPDIR_CERT/preflight.json")"

[[ "$FACE_TOTAL" -gt 0 && "$FACE_APPROVED" -eq "$FACE_TOTAL" ]] || fail "Face cohort is not fully approved ($FACE_APPROVED/$FACE_TOTAL)"
[[ "$AUDIO_TOTAL" -gt 0 && "$AUDIO_APPROVED" -eq "$AUDIO_TOTAL" ]] || fail "Audio cohort is not fully approved ($AUDIO_APPROVED/$AUDIO_TOTAL)"
[[ "$FUSION_TOTAL" -gt 0 ]] || fail "no Fusion scene stage exists"

echo "PASS: Face $FACE_APPROVED/$FACE_TOTAL • Audio $AUDIO_APPROVED/$AUDIO_TOTAL • Fusion scenes $FUSION_TOTAL"

STAGE_ID="$(jq -r '[.stages[] | select(.stage_type=="fusion" and .scope_type=="scene" and .state!="approved")][0].stage_run_id // [.stages[] | select(.stage_type=="fusion" and .scope_type=="scene")][0].stage_run_id // ""' "$TMPDIR_CERT/workflow.json")"
[[ -n "$STAGE_ID" ]] || fail "unable to identify Fusion scene stage"

STAGE_STATE="$(jq -r --arg id "$STAGE_ID" '.stages[] | select(.stage_run_id==$id) | .state' "$TMPDIR_CERT/workflow.json")"
echo "Fusion stage: $STAGE_ID ($STAGE_STATE)"

info "2. EXISTING svc-pricing PREVIEW — NO GENERATION"
PREVIEW_JSON="$(api_post "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/pricing-preview" '{"external_provider_ok":true}')" || fail "Fusion pricing preview failed"
printf '%s' "$PREVIEW_JSON" > "$TMPDIR_CERT/preview.json"

TURN_COUNT="$(jq -r '.turn_count // 0' "$TMPDIR_CERT/preview.json")"
QUOTE_COUNT="$(jq -r '.children | length' "$TMPDIR_CERT/preview.json")"
[[ "$TURN_COUNT" -gt 0 ]] || fail "Fusion preview returned no dialogue turns"

if [[ "$QUOTE_COUNT" -gt 0 ]]; then
  jq -e 'all(.children[]; ((.quote_id // "") | length) > 0 and ((.request_nonce // "") | length) > 0)' "$TMPDIR_CERT/preview.json" >/dev/null \
    || fail "one or more Fusion child previews are missing quote_id/request_nonce"

  echo "PASS: $QUOTE_COUNT/$TURN_COUNT child render quote(s) returned by existing svc-fusion/svc-pricing"
  echo "Pricing summaries:"
  jq -r '.children[] | "  #\(.sequence_no) \(.display_name): " + ((.pricing_summary.display_total // .pricing_summary.estimated_credits_label // .pricing.summary.display_total // .message // "quote ready")|tostring)' "$TMPDIR_CERT/preview.json"
else
  [[ "$STAGE_STATE" == "failed" ]] || fail "empty quote bundle is valid only for a failed stitch-only recovery"
  echo "PASS: zero new child quotes required — all prior child renders are reusable; stitch-only recovery is available"
fi

if [[ "$EXECUTE_BILLABLE" != "1" ]]; then
  echo
  echo "PASS: non-billable certification complete. No generation was started."
  echo "To execute the confirmed provider path, rerun with EXECUTE_BILLABLE=1."
  exit 0
fi

info "3. EXPLICIT CONFIRMATION + GENERATION"
if [[ "$QUOTE_COUNT" -eq 0 ]]; then
  DISPATCH_JSON="$(api_post "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/retry-stitch" '{}')" \
    || fail "stitch-only recovery dispatch failed"
  printf '%s' "$DISPATCH_JSON" > "$TMPDIR_CERT/dispatch.json"
  [[ "$(jq -r '.retry_scope // ""' "$TMPDIR_CERT/dispatch.json")" == "stitch_only" ]] || fail "retry was not stitch_only"
  NEW_CHARGES="$(jq -r '.new_child_charges // 0' "$TMPDIR_CERT/dispatch.json")"
  [[ "$NEW_CHARGES" == "0" ]] || fail "stitch-only retry reported a new child charge"
  echo "PASS: stitch-only attempt created with zero new child charges"
else
  CONFIRMATIONS="$(jq -c '[.children[] | {dialogue_turn_id,request_nonce,quote_id,preview_fingerprint}]' "$TMPDIR_CERT/preview.json")"
  BODY="$(jq -cn --argjson confirmations "$CONFIRMATIONS" '{confirmations:$confirmations,external_provider_ok:true,user_confirmed:true}')"
  DISPATCH_JSON="$(api_post "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/dispatch" "$BODY")" \
    || fail "Fusion dispatch failed after explicit confirmation"
  printf '%s' "$DISPATCH_JSON" > "$TMPDIR_CERT/dispatch.json"
  [[ "$(jq -r '.stage_state // ""' "$TMPDIR_CERT/dispatch.json")" == "generating" ]] || fail "Fusion dispatch did not enter generating"
  echo "PASS: user-confirmed quoted children dispatched"
fi

ATTEMPT_ID="$(jq -r '.attempt_id // ""' "$TMPDIR_CERT/dispatch.json")"
[[ -n "$ATTEMPT_ID" ]] || fail "dispatch did not return attempt_id"

info "4. POLL COMPACT Fusion STATUS + ORDERED STITCH"
SUCCESS_JSON=""
for poll in $(seq 1 "$MAX_POLLS"); do
  set +e
  SYNC_JSON="$(api_post "/api/director/studio-workflows/$WORKFLOW_ID/fusion-stages/$STAGE_ID/sync" '{}' 2>"$TMPDIR_CERT/sync.err")"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    LATEST="$(api_get "/api/director/studio-workflows/$WORKFLOW_ID")" || true
    latest_state="$(printf '%s' "$LATEST" | jq -r --arg id "$STAGE_ID" '.stages[]? | select(.stage_run_id==$id) | .state // empty' 2>/dev/null || true)"
    if [[ "$latest_state" == "failed" ]]; then
      fail "Fusion scene entered failed state. Successful child outputs remain preserved for failed-child/stitch-only recovery."
    fi
    sleep "$POLL_SECONDS"
    continue
  fi

  printf '%s' "$SYNC_JSON" > "$TMPDIR_CERT/sync.json"
  stage_state="$(jq -r '.stage_state // ""' "$TMPDIR_CERT/sync.json")"
  provider_state="$(jq -r '.provider_state // ""' "$TMPDIR_CERT/sync.json")"
  child_total="$(jq -r '.children | length' "$TMPDIR_CERT/sync.json")"
  child_done="$(jq -r '[.children[]? | select((.status=="succeeded") or (.status=="completed") or (.status=="complete") or (.status=="ready"))] | length' "$TMPDIR_CERT/sync.json")"
  echo "poll $poll: scene=$stage_state provider=$provider_state children=$child_done/$child_total"

  if [[ "$stage_state" == "awaiting_review" || "$stage_state" == "approved" ]]; then
    SUCCESS_JSON="$SYNC_JSON"
    break
  fi
  if [[ "$stage_state" == "failed" ]]; then
    fail "Fusion scene failed; successful children are preserved for scoped recovery"
  fi
  sleep "$POLL_SECONDS"
done

[[ -n "$SUCCESS_JSON" ]] || fail "Fusion did not reach reviewable output within $((MAX_POLLS * POLL_SECONDS)) seconds"
printf '%s' "$SUCCESS_JSON" > "$TMPDIR_CERT/success.json"

MEDIA_ID="$(jq -r '.media_asset_id // ""' "$TMPDIR_CERT/success.json")"
VIDEO_URL="$(jq -r '.video_url // ""' "$TMPDIR_CERT/success.json")"
REVIEW_ID="$(jq -r '.review_item_id // ""' "$TMPDIR_CERT/success.json")"
[[ -n "$MEDIA_ID" ]] || fail "reviewable Fusion output has no media_asset_id"
[[ -n "$VIDEO_URL" ]] || fail "reviewable Fusion output has no video_url"
[[ -n "$REVIEW_ID" ]] || fail "reviewable Fusion output has no review_item_id"

echo "PASS: ordered scene video created and awaiting human review"
echo "media_asset_id=$MEDIA_ID"
echo "review_item_id=$REVIEW_ID"

info "5. CANONICAL ATTEMPT INTEGRITY"
if [[ -f "$ROOT/infra/.env" ]]; then
  POSTGRES_DB="$(awk -F= '$1=="POSTGRES_DB"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
  POSTGRES_USER="$(awk -F= '$1=="POSTGRES_USER"{sub(/^[^=]*=/,""); print; exit}' "$ROOT/infra/.env")"
  ATTEMPT_ROW="$("${COMPOSE[@]}" exec -T desifaces-db psql -At -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select state,completed_at is not null,media_id::text from public.v3_studio_stage_attempts where attempt_id='$ATTEMPT_ID'::uuid")"
  IFS='|' read -r ATTEMPT_STATE ATTEMPT_COMPLETED ATTEMPT_MEDIA <<< "$ATTEMPT_ROW"
  [[ "$ATTEMPT_STATE" == "succeeded" ]] || fail "attempt state is $ATTEMPT_STATE, expected succeeded"
  [[ "$ATTEMPT_COMPLETED" == "t" ]] || fail "attempt completed_at was not recorded"
  [[ "$ATTEMPT_MEDIA" == "$MEDIA_ID" ]] || fail "attempt media lineage does not match reviewable media"
  echo "PASS: attempt terminal state/completed_at/media lineage are canonical"
else
  echo "SKIP: infra/.env unavailable; DB integrity assertion not run"
fi

info "6. HUMAN OWNERSHIP GATE"
echo "PASS: Fusion provider + pricing + stitch path is certified through the HITL boundary."
echo "The script intentionally does NOT approve the scene. The creator must review the video in desifaces and choose Approve or Revise."
echo "After creator approval, workflow certification requires state=completed and final_media_id != null."
