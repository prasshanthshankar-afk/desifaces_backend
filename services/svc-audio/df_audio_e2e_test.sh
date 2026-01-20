#!/usr/bin/env bash
set -euo pipefail

# --------------------------------------------
# DesiFaces - svc-audio end-to-end smoke test
# --------------------------------------------
# UI Inputs -> POST /api/audio/tts -> worker synth+translate -> upload -> status -> download artifact

AUDIO_BASE="${AUDIO_BASE:-http://localhost:8004}"
: "${DF_TOKEN:?DF_TOKEN is required}"

TEXT="${TEXT:-hello}"
TRANSLATE="${TRANSLATE:-true}"
SOURCE_LANGUAGE="${SOURCE_LANGUAGE:-en}"
INPUT_LANGUAGE="${INPUT_LANGUAGE:-}"          # optional
TARGET_LOCALE="${TARGET_LOCALE:-hi-IN}"
OUTPUT_FORMAT="${OUTPUT_FORMAT:-mp3}"

VOICE="${VOICE:-}"                             # optional
RATE="${RATE:-}"                               # optional
PITCH="${PITCH:-}"                             # optional
STYLE="${STYLE:-}"                             # optional
EMOTION="${EMOTION:-}"                         # optional
VOLUME="${VOLUME:-}"                           # optional
STYLE_DEGREE="${STYLE_DEGREE:-}"               # optional
CONTEXT="${CONTEXT:-}"                         # optional (if empty, script injects nonce to avoid dedupe)

POLL_SECONDS="${POLL_SECONDS:-1}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"

AUTH=(-H "Authorization: Bearer ${DF_TOKEN}" -H "Content-Type: application/json")

uuid4() {
  python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
}

now_ms() {
  python3 - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

bool_norm() {
  local v="${1:-}"
  v="$(echo "$v" | tr '[:upper:]' '[:lower:]' | xargs)"
  if [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" || "$v" == "y" ]]; then
    echo "true"
  else
    echo "false"
  fi
}

json_escape() {
  # Escapes a string for JSON value context.
  # IMPORTANT: we must pass $1 into python or it will always be empty.
  python3 - "$1" <<'PY'
import json, sys
print(json.dumps(sys.argv[1]))
PY
}

require_non_empty() {
  local name="$1"
  local val="$2"
  if [[ -z "${val}" ]]; then
    echo "ERROR: required input '${name}' is empty"
    exit 1
  fi
}

# Avoid server-side dedupe: if no context provided, inject a nonce.
if [[ -z "${CONTEXT}" ]]; then
  CONTEXT="nonce:$(uuid4)"
fi

TRANSLATE="$(bool_norm "$TRANSLATE")"

# Fail fast on required UI inputs
require_non_empty "TEXT" "$TEXT"
require_non_empty "SOURCE_LANGUAGE" "$SOURCE_LANGUAGE"
require_non_empty "TARGET_LOCALE" "$TARGET_LOCALE"
require_non_empty "OUTPUT_FORMAT" "$OUTPUT_FORMAT"
require_non_empty "CONTEXT" "$CONTEXT"

echo "=============================="
echo "DesiFaces svc-audio E2E Test"
echo "=============================="
echo "AUDIO_BASE     : ${AUDIO_BASE}"
echo "UI Inputs:"
echo "  text         : ${TEXT}"
echo "  translate    : ${TRANSLATE}"
echo "  source_lang  : ${SOURCE_LANGUAGE}"
[[ -n "${INPUT_LANGUAGE}" ]] && echo "  input_lang   : ${INPUT_LANGUAGE}"
echo "  target_locale: ${TARGET_LOCALE}"
echo "  output_format: ${OUTPUT_FORMAT}"
[[ -n "${VOICE}" ]]        && echo "  voice        : ${VOICE}"
[[ -n "${RATE}" ]]         && echo "  rate         : ${RATE}"
[[ -n "${PITCH}" ]]        && echo "  pitch        : ${PITCH}"
[[ -n "${STYLE}" ]]        && echo "  style        : ${STYLE}"
[[ -n "${EMOTION}" ]]      && echo "  emotion      : ${EMOTION}"
[[ -n "${VOLUME}" ]]       && echo "  volume       : ${VOLUME}"
[[ -n "${STYLE_DEGREE}" ]] && echo "  style_degree : ${STYLE_DEGREE}"
echo "  context      : ${CONTEXT}"
echo "------------------------------"

# Build JSON payload safely (string-escape via python json.dumps)
payload="{"
payload+="\"text\":$(json_escape "$TEXT")"
payload+=",\"translate\":${TRANSLATE}"
payload+=",\"source_language\":$(json_escape "$SOURCE_LANGUAGE")"
payload+=",\"target_locale\":$(json_escape "$TARGET_LOCALE")"
payload+=",\"output_format\":$(json_escape "$OUTPUT_FORMAT")"
payload+=",\"context\":$(json_escape "$CONTEXT")"

if [[ -n "$INPUT_LANGUAGE" ]]; then
  payload+=",\"input_language\":$(json_escape "$INPUT_LANGUAGE")"
fi
if [[ -n "$VOICE" ]]; then
  payload+=",\"voice\":$(json_escape "$VOICE")"
fi
if [[ -n "$RATE" ]]; then
  payload+=",\"rate\":${RATE}"
fi
if [[ -n "$PITCH" ]]; then
  payload+=",\"pitch\":${PITCH}"
fi
if [[ -n "$STYLE" ]]; then
  payload+=",\"style\":$(json_escape "$STYLE")"
fi
if [[ -n "$EMOTION" ]]; then
  payload+=",\"emotion\":$(json_escape "$EMOTION")"
fi
if [[ -n "$VOLUME" ]]; then
  payload+=",\"volume\":$(json_escape "$VOLUME")"
fi
if [[ -n "$STYLE_DEGREE" ]]; then
  payload+=",\"style_degree\":$(json_escape "$STYLE_DEGREE")"
fi

payload+="}"

echo "Request payload:"
echo "$payload" | jq

echo
echo "[1] Creating TTS job: POST /api/audio/tts"
CREATE_RESP="$(curl -sS -X POST "${AUDIO_BASE}/api/audio/tts" "${AUTH[@]}" -d "$payload")"
echo "$CREATE_RESP" | jq

JOB_ID="$(jq -r '.job_id // empty' <<< "$CREATE_RESP")"
if [[ -z "$JOB_ID" ]]; then
  echo "ERROR: job_id missing in create response"
  exit 1
fi

echo
echo "[2] Polling status: GET /api/audio/jobs/${JOB_ID}/status"
START_MS="$(now_ms)"
STATUS_RESP=""
STATUS=""

while true; do
  STATUS_RESP="$(curl -sS "${AUDIO_BASE}/api/audio/jobs/${JOB_ID}/status" -H "Authorization: Bearer ${DF_TOKEN}")"
  STATUS="$(jq -r '.status // empty' <<< "$STATUS_RESP")"

  if [[ "$STATUS" == "succeeded" ]]; then
    echo "status=succeeded"
    break
  fi
  if [[ "$STATUS" == "failed" ]]; then
    echo "status=failed"
    echo "$STATUS_RESP" | jq
    exit 1
  fi

  NOW_MS="$(now_ms)"
  ELAPSED_SEC=$(( (NOW_MS - START_MS) / 1000 ))
  if (( ELAPSED_SEC >= TIMEOUT_SECONDS )); then
    echo "ERROR: timeout waiting for job to finish (status=$STATUS)"
    echo "$STATUS_RESP" | jq
    exit 1
  fi

  echo "status=${STATUS:-unknown} (t=${ELAPSED_SEC}s)"
  sleep "$POLL_SECONDS"
done

echo
echo "[3] Validating payload fields (UI -> resolved)"
echo "$STATUS_RESP" | jq '.payload'

VOICE_OUT="$(jq -r '.payload.voice // empty' <<< "$STATUS_RESP")"
FINAL_TEXT="$(jq -r '.payload.final_synthesis_text // empty' <<< "$STATUS_RESP")"
TRANSLATE_OUT="$(jq -r '.payload.translate // false' <<< "$STATUS_RESP")"

if [[ -z "$VOICE_OUT" ]]; then
  echo "ERROR: payload.voice is empty (expected resolved voice)"
  exit 1
fi

if [[ "$TRANSLATE_OUT" == "true" && -z "$FINAL_TEXT" ]]; then
  echo "ERROR: translate=true but payload.final_synthesis_text is empty"
  exit 1
fi

echo "OK: resolved voice = ${VOICE_OUT}"
[[ -n "$FINAL_TEXT" ]] && echo "OK: final_synthesis_text = ${FINAL_TEXT}"

echo
echo "[4] Downloading artifact and verifying file"
AUDIO_URL="$(jq -r '.variants[0].audio_url // empty' <<< "$STATUS_RESP")"
CONTENT_TYPE="$(jq -r '.variants[0].content_type // empty' <<< "$STATUS_RESP")"
BYTES_REPORTED="$(jq -r '.variants[0].bytes // 0' <<< "$STATUS_RESP")"

if [[ -z "$AUDIO_URL" ]]; then
  echo "ERROR: variants[0].audio_url missing"
  exit 1
fi

OUT_PATH="/tmp/df_audio_${JOB_ID}.mp3"
curl -sS -L -o "$OUT_PATH" "$AUDIO_URL"

if [[ ! -f "$OUT_PATH" ]]; then
  echo "ERROR: download failed: $OUT_PATH not found"
  exit 1
fi

BYTES_LOCAL="$(stat -c%s "$OUT_PATH" 2>/dev/null || stat -f%z "$OUT_PATH")"

echo "Downloaded: $OUT_PATH"
echo "Reported bytes: $BYTES_REPORTED"
echo "Local bytes   : $BYTES_LOCAL"
echo "Reported type : $CONTENT_TYPE"
file "$OUT_PATH"

if (( BYTES_LOCAL < 1000 )); then
  echo "ERROR: downloaded file too small: ${BYTES_LOCAL} bytes"
  exit 1
fi

if [[ -n "$BYTES_REPORTED" && "$BYTES_REPORTED" != "0" ]]; then
  DIFF=$(( BYTES_LOCAL > BYTES_REPORTED ? BYTES_LOCAL - BYTES_REPORTED : BYTES_REPORTED - BYTES_LOCAL ))
  if (( DIFF > 512 )); then
    echo "WARN: bytes mismatch >512 (reported=$BYTES_REPORTED, local=$BYTES_LOCAL)"
  fi
fi

if ! file "$OUT_PATH" | grep -qiE "MPEG|layer III|MP3"; then
  echo "ERROR: downloaded file does not look like MP3"
  exit 1
fi

echo
echo "=============================="
echo "E2E PASS ✅  job_id=${JOB_ID}"
echo "=============================="


TEST_DEDUPE="${TEST_DEDUPE:-0}"

if [[ "$TEST_DEDUPE" == "1" ]]; then
  echo
  echo "=============================="
  echo "DEDUPE / IDEMPOTENCY TESTS"
  echo "=============================="

  echo "[D1] Re-submit SAME payload; expect SAME job_id"
  CREATE_RESP2="$(curl -sS -X POST "${AUDIO_BASE}/api/audio/tts" "${AUTH[@]}" -d "$payload")"
  JOB_ID2="$(jq -r '.job_id // empty' <<< "$CREATE_RESP2")"
  echo "$CREATE_RESP2" | jq

  if [[ "$JOB_ID2" != "$JOB_ID" ]]; then
    echo "ERROR: dedupe failed; expected same job_id"
    echo "  job_id_1=$JOB_ID"
    echo "  job_id_2=$JOB_ID2"
    exit 1
  fi
  echo "OK: dedupe returned same job_id = $JOB_ID2"

  echo "[D2] Submit payload with NEW nonce; expect NEW job_id"
  NEW_CONTEXT="nonce:$(uuid4)"
  payload_nonce="$(echo "$payload" | jq --arg c "$NEW_CONTEXT" '.context=$c')"

  CREATE_RESP3="$(curl -sS -X POST "${AUDIO_BASE}/api/audio/tts" "${AUTH[@]}" -d "$payload_nonce")"
  JOB_ID3="$(jq -r '.job_id // empty' <<< "$CREATE_RESP3")"
  echo "$CREATE_RESP3" | jq

  if [[ -z "$JOB_ID3" ]]; then
    echo "ERROR: missing job_id for nonce payload"
    exit 1
  fi

  if [[ "$JOB_ID3" == "$JOB_ID" ]]; then
    echo "ERROR: nonce did not bypass dedupe; expected new job_id"
    echo "  job_id_original=$JOB_ID"
    echo "  job_id_nonce=$JOB_ID3"
    exit 1
  fi

  echo "OK: nonce bypass produced new job_id = $JOB_ID3"
fi