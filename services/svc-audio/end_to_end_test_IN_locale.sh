#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://localhost:8004}"
AUTH="${AUTH:-http://localhost:8000}"

TARGET_LOCALE="${TARGET_LOCALE:-ta-IN}"
SOURCE_LANGUAGE="${SOURCE_LANGUAGE:-en}"
TEXT="${TEXT:-Vanakkam! This is a DesiFaces Audio Studio API test.}"
OUTPUT_FORMAT="${OUTPUT_FORMAT:-mp3}"

EXPECTED_OUTCOME="${EXPECTED_OUTCOME:-succeeded}"   # succeeded | blocked
EXPECTED_BLOCK_CODE="${EXPECTED_BLOCK_CODE:-PRICING_INSUFFICIENT_CREDITS}"

: "${AUTH_EMAIL:?Set AUTH_EMAIL env var}"
: "${AUTH_PASSWORD:?Set AUTH_PASSWORD env var}"

OUT_DIR="${OUT_DIR:-/tmp/df_audio_pricing_e2e_$(date +%s)}"
mkdir -p "$OUT_DIR"

CREATE_JSON="$OUT_DIR/create.json"
FINAL_JSON="$OUT_DIR/final.json"
HEAD_TXT="$OUT_DIR/head.txt"

echo "OUT_DIR=$OUT_DIR"

echo
echo "=== sanity: svc-audio openapi has /api/audio/tts ==="
curl -sS "$BASE/openapi.json" | jq -e '.paths["/api/audio/tts"]' >/dev/null
echo "OK: /api/audio/tts exists on $BASE"

echo
echo "=== sanity: locales (should include India locales) ==="
curl -sS "$BASE/api/audio/catalog/locales" | jq -r '.items[].locale' | head -n 20 || true

echo
echo "=== login: get JWT from svc-core ==="
TOKEN="$(
  curl -sS -X POST "$AUTH/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$AUTH_EMAIL\",\"password\":\"$AUTH_PASSWORD\"}" \
  | jq -r '.access_token // .token // .data.access_token // empty'
)"

if [[ -z "$TOKEN" ]]; then
  echo "Login did not return a token. Printing response:"
  curl -sS -X POST "$AUTH/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$AUTH_EMAIL\",\"password\":\"$AUTH_PASSWORD\"}" | jq
  exit 1
fi

echo "OK: TOKEN=${TOKEN:0:20}..."

echo
echo "=== create tts job ==="
CREATE_HTTP="$(
  curl -sS -o "$CREATE_JSON" -w "%{http_code}" \
    -X POST "$BASE/api/audio/tts" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
      \"text\": $(jq -Rn --arg v "$TEXT" '$v'),
      \"target_locale\": $(jq -Rn --arg v "$TARGET_LOCALE" '$v'),
      \"source_language\": $(jq -Rn --arg v "$SOURCE_LANGUAGE" '$v'),
      \"translate\": true,
      \"voice\": null,
      \"style\": null,
      \"style_degree\": null,
      \"rate\": 1.0,
      \"pitch\": 0.0,
      \"volume\": 1.0,
      \"context\": null,
      \"output_format\": $(jq -Rn --arg v "$OUTPUT_FORMAT" '$v')
    }"
)"

echo "CREATE_HTTP=$CREATE_HTTP"
cat "$CREATE_JSON" | jq .

if [[ "$CREATE_HTTP" != "200" && "$CREATE_HTTP" != "201" ]]; then
  echo "❌ create returned non-2xx"
  exit 2
fi

JOB_ID="$(jq -r '.job_id // empty' "$CREATE_JSON")"
CREATE_STATUS="$(jq -r '.status // empty' "$CREATE_JSON")"
CREATE_ERR_CODE="$(jq -r '.error_code // empty' "$CREATE_JSON")"

if [[ -z "$JOB_ID" ]]; then
  echo "❌ job_id missing in create response"
  exit 3
fi

echo "OK: JOB_ID=$JOB_ID"
echo "CREATE_STATUS=$CREATE_STATUS"

echo
echo "=== poll job status ==="
FINAL=""
for i in $(seq 1 60); do
  R="$(curl -sS "$BASE/api/audio/jobs/$JOB_ID/status" -H "Authorization: Bearer $TOKEN")"
  ST="$(echo "$R" | jq -r '.status // ""' | tr '[:upper:]' '[:lower:]')"
  echo "[$i] status=$ST"

  if [[ "$ST" == "succeeded" || "$ST" == "failed" || "$ST" == "blocked" || "$ST" == "cancelled" || "$ST" == "canceled" ]]; then
    FINAL="$R"
    break
  fi

  sleep 1
done

if [[ -z "$FINAL" ]]; then
  echo "❌ did not reach terminal state within timeout"
  exit 4
fi

echo "$FINAL" > "$FINAL_JSON"

echo
echo "=== final status json ==="
cat "$FINAL_JSON" | jq .

FINAL_STATUS="$(jq -r '.status // empty' "$FINAL_JSON" | tr '[:upper:]' '[:lower:]')"
FINAL_ERR_CODE="$(jq -r '.error_code // empty' "$FINAL_JSON")"
AUDIO_URL="$(jq -r '.variants[0].audio_url // empty' "$FINAL_JSON")"

echo
echo "=== pricing snapshot (if present) ==="
jq '.pricing // .payload.pricing // {}' "$FINAL_JSON"

case "$EXPECTED_OUTCOME" in
  succeeded)
    if [[ "$FINAL_STATUS" != "succeeded" ]]; then
      echo "❌ expected succeeded, got status=$FINAL_STATUS error_code=$FINAL_ERR_CODE"
      exit 10
    fi

    if [[ -z "$AUDIO_URL" ]]; then
      echo "❌ variants[0].audio_url missing for succeeded job"
      exit 11
    fi

    echo
    echo "=== HEAD audio_url ==="
    curl -sS -I "$AUDIO_URL" | tee "$HEAD_TXT" | sed -n '1,25p'

    if ! grep -E '^HTTP/' "$HEAD_TXT" >/dev/null 2>&1; then
      echo "❌ HEAD did not return an HTTP status line"
      exit 12
    fi

    echo "✅ succeeded path validated"
    ;;

  blocked)
    if [[ "$FINAL_STATUS" != "blocked" ]]; then
      echo "❌ expected blocked, got status=$FINAL_STATUS error_code=$FINAL_ERR_CODE"
      exit 20
    fi

    if [[ "$FINAL_ERR_CODE" != "$EXPECTED_BLOCK_CODE" ]]; then
      echo "❌ expected error_code=$EXPECTED_BLOCK_CODE, got $FINAL_ERR_CODE"
      exit 21
    fi

    if [[ -n "$AUDIO_URL" ]]; then
      echo "❌ blocked job should not have audio_url"
      exit 22
    fi

    echo "✅ blocked path validated"
    ;;

  *)
    echo "❌ unsupported EXPECTED_OUTCOME=$EXPECTED_OUTCOME"
    exit 30
    ;;
esac