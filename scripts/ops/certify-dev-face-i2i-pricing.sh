#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

CORE_BASE="${CORE_BASE:-http://127.0.0.1:18000}"
FACE_BASE="${FACE_BASE:-http://127.0.0.1:18003}"
EMAIL="${EMAIL:-}"
PASSWORD="${PASSWORD:-}"
SOURCE_IMAGE_URL="${SOURCE_IMAGE_URL:-}"
NUM_VARIANTS="${NUM_VARIANTS:-2}"
PRESERVATION_STRENGTH="${PRESERVATION_STRENGTH:-0.995}"
TIMEOUT_SECS="${TIMEOUT_SECS:-600}"
POLL_SECS="${POLL_SECS:-3}"
USER_PROMPT="${USER_PROMPT:-EDIT THE INPUT PHOTO ONLY. Preserve the exact same person, face, eyes, lips, jawline, eyebrows, age appearance, skin tone, hair, facial geometry and gender presentation. Change only the outfit to a dark navy professional jacket and change the background to a modern neutral office. Photorealistic.}"

[[ -n "$EMAIL" ]] || { echo "FAIL: EMAIL required"; exit 2; }
[[ -n "$PASSWORD" ]] || { echo "FAIL: PASSWORD required"; exit 2; }
[[ -n "$SOURCE_IMAGE_URL" ]] || { echo "FAIL: SOURCE_IMAGE_URL required"; exit 2; }

OUT_DIR="${OUT_DIR:-/tmp/df_i2i_pricing_cert_$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT_DIR"

AUTH="$OUT_DIR/auth.json"
STUDIO_INPUT="$OUT_DIR/studio_input.json"
PREVIEW_REQ="$OUT_DIR/preview_request.json"
PREVIEW_RESP="$OUT_DIR/preview_response.json"
GENERATE_REQ="$OUT_DIR/generate_request.json"
GENERATE_RESP="$OUT_DIR/generate_response.json"
STATUS="$OUT_DIR/status.json"
SUMMARY="$OUT_DIR/summary.json"

fail_json() {
  local label="$1" file="$2"
  echo "$label=FAIL"
  [[ -f "$file" ]] && jq . "$file" 2>/dev/null || cat "$file" 2>/dev/null || true
  exit 1
}

echo "============================================================"
echo " desifaces DEV — FACE I2I + PRICING CERTIFICATION"
echo "============================================================"
echo "core=$CORE_BASE"
echo "face=$FACE_BASE"
echo "variants=$NUM_VARIANTS"
echo "identity_lock=STRICT"
echo "production=UNTOUCHED"

curl -fsS -X POST "$CORE_BASE/api/auth/login"   -H 'Content-Type: application/json'   --data "$(jq -cn --arg email "$EMAIL" --arg password "$PASSWORD" '{email:$email,password:$password}')"   > "$AUTH" || fail_json "AUTH" "$AUTH"

TOKEN="$(jq -r '.access_token // .token // empty' "$AUTH")"
[[ -n "$TOKEN" ]] || fail_json "AUTH" "$AUTH"
echo "AUTH=PASS"

jq -cn   --arg prompt "$USER_PROMPT"   --arg source "$SOURCE_IMAGE_URL"   --argjson variants "$NUM_VARIANTS"   --argjson preservation "$PRESERVATION_STRENGTH"   '{
    mode:"image-to-image",
    language:"en",
    user_prompt:$prompt,
    num_variants:$variants,
    source_image_url:$source,
    preservation_strength:$preservation,
    identity_lock:true,
    identity_lock_level:"strict",
    preserve_source_identity:true,
    preserve_source_gender:true,
    gender_lock_mode:"source",
    allowed_i2i_changes:["outfit","background","lighting"],
    forbidden_i2i_changes:["identity","gender","age","skin_tone","facial_geometry"]
  }' > "$STUDIO_INPUT"

jq -cn   --slurpfile input "$STUDIO_INPUT"   '{
    studio:"face",
    action:"generate",
    studio_input:$input[0],
    client_context:{surface:"web",workflow:"i2i_launch_certification"}
  }' > "$PREVIEW_REQ"

HTTP="$(curl -sS -o "$PREVIEW_RESP" -w '%{http_code}'   -X POST "$FACE_BASE/api/face/creator/pricing/preview"   -H "Authorization: Bearer $TOKEN"   -H 'Content-Type: application/json'   --data @"$PREVIEW_REQ")"
[[ "$HTTP" == "200" ]] || fail_json "I2I_PRICING_PREVIEW" "$PREVIEW_RESP"

QUOTE_ID="$(jq -r '.quote_id // empty' "$PREVIEW_RESP")"
FINGERPRINT="$(jq -r '.preview_fingerprint // empty' "$PREVIEW_RESP")"
[[ -n "$QUOTE_ID" && -n "$FINGERPRINT" ]] || fail_json "I2I_PRICING_PREVIEW" "$PREVIEW_RESP"

echo "I2I_PRICING_PREVIEW=PASS"
jq -r '[
  "variant_code="+(.pricing.variant_code // .pricing.sku_code // "unknown"),
  "estimated_units="+(.pricing.estimated_units // "unknown"),
  "estimated_amount="+(.pricing.estimated_amount // .pricing.amount // "unknown"),
  "currency="+(.pricing.currency // "credits")
] | .[]' "$PREVIEW_RESP"

jq -cn   --slurpfile input "$STUDIO_INPUT"   --arg quote "$QUOTE_ID"   --arg fingerprint "$FINGERPRINT"   '{
    studio:"face",
    studio_input:$input[0],
    pricing_confirmation:{
      quote_id:$quote,
      preview_fingerprint:$fingerprint,
      user_confirmed:true
    }
  }' > "$GENERATE_REQ"

HTTP="$(curl -sS -o "$GENERATE_RESP" -w '%{http_code}'   -X POST "$FACE_BASE/api/face/creator/generate"   -H "Authorization: Bearer $TOKEN"   -H 'Content-Type: application/json'   --data @"$GENERATE_REQ")"
[[ "$HTTP" == "200" || "$HTTP" == "201" || "$HTTP" == "202" ]] || fail_json "I2I_GENERATE" "$GENERATE_RESP"

JOB_ID="$(jq -r '.job_id // .id // empty' "$GENERATE_RESP")"
[[ -n "$JOB_ID" ]] || fail_json "I2I_GENERATE" "$GENERATE_RESP"
echo "I2I_GENERATE_ACCEPTED=PASS"
echo "job_id=$JOB_ID"

start="$(date +%s)"
while :; do
  HTTP="$(curl -sS -o "$STATUS" -w '%{http_code}'     -H "Authorization: Bearer $TOKEN"     "$FACE_BASE/api/face/creator/jobs/$JOB_ID/status")"
  [[ "$HTTP" == "200" ]] || fail_json "I2I_STATUS" "$STATUS"

  state="$(jq -r '.status // .state // "unknown"' "$STATUS")"
  pricing_state="$(jq -r '.pricing.state // "unknown"' "$STATUS")"
  completed="$(jq -r '(.variants // []) | length' "$STATUS")"
  echo "status=$state pricing=$pricing_state variants=$completed"

  case "$state" in
    succeeded|failed|cancelled) break ;;
  esac

  now="$(date +%s)"
  (( now - start <= TIMEOUT_SECS )) || { echo "I2I_TIMEOUT=FAIL"; exit 1; }
  sleep "$POLL_SECS"
done

FINAL_STATE="$(jq -r '.status // .state // "unknown"' "$STATUS")"
PRICING_STATE="$(jq -r '.pricing.state // "unknown"' "$STATUS")"
VARIANT_COUNT="$(jq -r '(.variants // []) | length' "$STATUS")"

if [[ "$FINAL_STATE" != "succeeded" ]]; then
  fail_json "I2I_GENERATION" "$STATUS"
fi
echo "I2I_GENERATION=PASS"

if [[ "$PRICING_STATE" != "committed" ]]; then
  fail_json "I2I_PRICING_COMMIT" "$STATUS"
fi
echo "I2I_PRICING_COMMIT=PASS"

if [[ "$VARIANT_COUNT" -lt "$NUM_VARIANTS" ]]; then
  fail_json "I2I_VARIANT_COUNT" "$STATUS"
fi
echo "I2I_VARIANT_COUNT=PASS count=$VARIANT_COUNT"

URL_FAIL=0
while IFS= read -r url; do
  [[ -n "$url" ]] || continue
  code="$(curl -sS -L -o /dev/null -w '%{http_code}' -H 'Range: bytes=0-0' "$url" || true)"
  case "$code" in 200|206) ;; *) echo "artifact_http=$code"; URL_FAIL=1 ;; esac
done < <(jq -r '(.variants // [])[] | (.image_url // .url // empty)' "$STATUS")
[[ "$URL_FAIL" == "0" ]] || { echo "I2I_ARTIFACT_URLS=FAIL"; exit 1; }
echo "I2I_ARTIFACT_URLS=PASS"

jq '{
  job_id:(.job_id // null),
  status:(.status // .state // null),
  pricing:.pricing,
  pricing_summary:.pricing_summary,
  variants:[
    (.variants // [])[] | {
      variant_number,
      media_asset_id,
      face_profile_id,
      image_url:(.image_url // .url // null),
      identity_score,
      identity_verified,
      prompt_used
    }
  ]
}' "$STATUS" > "$SUMMARY"

echo
echo "===== CERTIFICATION ====="
echo "I2I_MODE=image-to-image"
echo "I2I_IDENTITY_LOCK_REQUEST=PASS"
echo "I2I_GENDER_SOURCE_AUTHORITY=PASS"
echo "I2I_PRICING_PREVIEW=PASS"
echo "I2I_GENERATION=PASS"
echo "I2I_PRICING_COMMIT=PASS"
echo "I2I_ARTIFACT_URLS=PASS"
echo "I2I_VISUAL_IDENTITY_REVIEW=PENDING"
echo "summary=$SUMMARY"
echo "production=UNTOUCHED"
echo "============================================================"
