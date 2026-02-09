#!/usr/bin/env bash
# services/svc-music/app/app/scripts/e2e/df_e2e_music_to_video.sh
set -euo pipefail

########################################
# Inputs (ONLY required)
########################################
: "${EMAIL:?Set EMAIL, e.g. user2@desifaces.ai}"
: "${PASSWORD:?Set PASSWORD, e.g. password2}"

# Optional (later once agentic AI is wired end-to-end)
INTENT="${INTENT:-}"

########################################
# Service bases (override if needed)
########################################
CORE_BASE="${CORE_BASE:-http://localhost:8000}"             # svc-core
MUSIC_BASE="${MUSIC_BASE:-http://localhost:8007}"           # svc-music
FUSION_BASE="${FUSION_BASE:-http://localhost:8002}"         # svc-fusion
FUSION_EXT_BASE="${FUSION_EXT_BASE:-http://localhost:8006}" # svc-fusion-extension (currently longform only)

CORE_API="${CORE_API:-$CORE_BASE/api}"
MUSIC_API="${MUSIC_API:-$MUSIC_BASE/api}"

# svc-fusion has paths "/jobs" (no /api prefix based on your openapi)
FUSION_JOBS_URL="${FUSION_JOBS_URL:-$FUSION_BASE/jobs}"

########################################
# Polling
########################################
POLL_SECONDS="${POLL_SECONDS:-3}"
POLL_MAX_TRIES="${POLL_MAX_TRIES:-120}"  # 6 minutes default

########################################
# Helpers
########################################
log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"; }
die() { echo "ERROR: $*" >&2; exit 2; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

require_cmd curl
require_cmd jq
require_cmd python3
require_cmd base64

RUN_DIR="${RUN_DIR:-/tmp/df_e2e_music_to_video_$(date +%s)}"
mkdir -p "$RUN_DIR"
log "RUN_DIR=$RUN_DIR"

curl_json() {
  # curl_json METHOD URL JSON TOKEN OUTFILE
  local method="$1"; shift
  local url="$1"; shift
  local json="$1"; shift
  local token="${1:-}"; shift || true
  local out="$1"; shift

  local args=(-sS -o "$out" -w "%{http_code}" -X "$method" "$url" --connect-timeout 5 --max-time 60)
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  args+=(-H "Content-Type: application/json" --data "$json")
  curl "${args[@]}" || true
}

curl_get() {
  # curl_get URL TOKEN OUTFILE
  local url="$1"; shift
  local token="${1:-}"; shift || true
  local out="$1"; shift
  local args=(-sS -o "$out" -w "%{http_code}" -X GET "$url" --connect-timeout 5 --max-time 60)
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  curl "${args[@]}" || true
}

jq_first_token() {
  jq -r '
    .access_token? //
    .token? //
    .jwt? //
    .data.access_token? //
    .data.token? //
    empty
  ' 2>/dev/null
}

########################################
# OpenAPI-driven enum discovery (battle-hardened)
########################################
fetch_openapi() {
  local base="$1"
  local out="$2"
  curl -fsS "$base/openapi.json" -o "$out" || die "failed to fetch openapi.json from $base"
}

schema_name_from_ref() {
  local ref="$1"
  [[ -z "$ref" || "$ref" == "null" ]] && echo "" && return 0
  echo "${ref##*/}"
}

enum_default_for_prop() {
  # enum_default_for_prop OPENAPI_JSON PATH METHOD PROP FALLBACK
  local spec="$1"; shift
  local path="$1"; shift
  local method="$1"; shift
  local prop="$1"; shift
  local fallback="$1"; shift

  local ref schema val
  ref="$(jq -r --arg p "$path" --arg m "$method" \
      '.paths[$p][$m].requestBody.content["application/json"].schema["$ref"] // empty' \
      "$spec" 2>/dev/null || true)"
  schema="$(schema_name_from_ref "$ref")"

  if [[ -n "$schema" ]]; then
    val="$(jq -r --arg s "$schema" --arg k "$prop" \
        '.components.schemas[$s].properties[$k].enum[0] // empty' \
        "$spec" 2>/dev/null || true)"
    if [[ -n "$val" && "$val" != "null" ]]; then
      echo "$val"
      return 0
    fi
  fi

  echo "$fallback"
}

########################################
# Autofill helper for POST 422 "field required"
########################################
missing_fields_from_422() {
  jq -r '
    .detail? // [] |
    .[]? |
    select(.msg? | test("field required"; "i")) |
    (.loc[1]? // empty)
  ' "$1" 2>/dev/null | awk 'NF' || true
}

guess_value() {
  local f="$1"
  local fl; fl="$(echo "$f" | tr '[:upper:]' '[:lower:]')"

  if [[ "$fl" == "project_id" || "$fl" == "music_project_id" || "$fl" == "pid" ]]; then jq -nc --arg v "$PID" '$v'; return; fi
  if [[ "$fl" == *"project"* && "$fl" == *"id" ]]; then jq -nc --arg v "$PID" '$v'; return; fi
  if [[ "$fl" == "voice_ref_asset_id" || "$fl" == *"voice"*"_asset_id" ]]; then jq -nc --arg v "$VOICE_REF_ASSET_ID" '$v'; return; fi
  if [[ "$fl" == "intent" || "$fl" == "prompt" || "$fl" == "creative_brief" ]]; then jq -nc --arg v "${INTENT:-Create a short cinematic music video.}" '$v'; return; fi
  if [[ "$fl" == "language_hint" || "$fl" == "language" || "$fl" == "locale" ]]; then echo '"en"'; return; fi

  # IMPORTANT: API enum values are lowercase (autopilot/co_create/byo)
  if [[ "$fl" == "mode" ]]; then echo '"autopilot"'; return; fi

  if [[ "$fl" == "duet_layout" || "$fl" == *"layout"* ]]; then echo '"split_screen"'; return; fi
  if [[ "$fl" == "camera_edit" ]]; then echo '"beat_cut"'; return; fi

  if [[ "$fl" == *"audio"*"_url" || "$fl" == "audio_url" ]]; then jq -nc --arg v "${AUDIO_URL:-}" '$v'; return; fi
  if [[ "$fl" == *"image"*"_url" || "$fl" == "image_url" ]]; then jq -nc --arg v "${IMAGE_URL:-}" '$v'; return; fi

  echo '"e2e"'
}

post_with_autofill() {
  local url="$1"; shift
  local payload="$1"; shift
  local token="$1"; shift
  local out="$1"; shift

  local tmp_payload="$RUN_DIR/_payload.json"
  echo "$payload" > "$tmp_payload"

  for _ in $(seq 1 12); do
    local resp="$RUN_DIR/_resp.json"
    local code
    code="$(curl_json POST "$url" "$(cat "$tmp_payload")" "$token" "$resp")"

    if [[ "$code" == "200" || "$code" == "201" || "$code" == "202" ]]; then
      cp "$resp" "$out"
      cp "$tmp_payload" "${out%.json}.payload_used.json"
      return 0
    fi

    if [[ "$code" == "422" ]]; then
      local missing
      missing="$(missing_fields_from_422 "$resp" | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
      [[ -n "$missing" ]] || { cat "$resp" >&2; return 1; }

      local changed=0
      for f in $missing; do
        if jq -e --arg k "$f" 'has($k)' "$tmp_payload" >/dev/null 2>&1; then
          continue
        fi
        local gv; gv="$(guess_value "$f")"
        jq --arg k "$f" --argjson v "$gv" '. + {($k): $v}' "$tmp_payload" > "$tmp_payload.new"
        mv "$tmp_payload.new" "$tmp_payload"
        changed=1
      done
      [[ "$changed" == "1" ]] || { cat "$resp" >&2; return 1; }
      continue
    fi

    cat "$resp" >&2 || true
    return 1
  done
  return 1
}

poll_music_job() {
  local job_id="$1"
  local url="$MUSIC_API/music/jobs/$job_id/status"
  for i in $(seq 1 "$POLL_MAX_TRIES"); do
    local out="$RUN_DIR/music_job_status.json"
    local code
    code="$(curl_get "$url" "$TOKEN" "$out")"
    if [[ "$code" == "200" ]]; then
      local st
      st="$(jq -r '.status? // .job_status? // .state? // .stage? // .data.status? // empty' "$out" 2>/dev/null || true)"
      [[ -z "$st" || "$st" == "null" ]] && st="unknown"
      log "svc-music job status ($i/$POLL_MAX_TRIES): $st"
      if echo "$st" | grep -Eqi 'succeeded|completed|done|ready|success'; then return 0; fi
      if echo "$st" | grep -Eqi 'failed|error|canceled|cancelled'; then cat "$out" >&2 || true; return 1; fi
    else
      log "WARN svc-music job status poll HTTP=$code"
    fi
    sleep "$POLL_SECONDS"
  done
  die "svc-music job polling timed out"
}

extract_audio_artifact_id() {
  local file="$1"
  python3 - <<PY
import json, re, sys
p="${file}"
UUID=re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
KEY_OK=re.compile(r"(audio|song|track).*?(artifact|asset)?_?id|artifact_?id.*?(audio|song|track)", re.I)
def walk(x):
  if isinstance(x, dict):
    for k,v in x.items():
      if isinstance(v,str) and UUID.match(v) and KEY_OK.search(str(k)):
        print(v); sys.exit(0)
      walk(v)
  elif isinstance(x, list):
    for v in x: walk(v)
with open(p,"r",encoding="utf-8") as f:
  j=json.load(f)
walk(j)
def walk_any(x):
  if isinstance(x, dict):
    for _,v in x.items():
      if isinstance(v,str) and UUID.match(v):
        print(v); sys.exit(0)
      walk_any(v)
  elif isinstance(x, list):
    for v in x: walk_any(v)
walk_any(j)
PY
}

make_1x1_png() {
  local out="$1"
  local b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7W2r0AAAAASUVORK5CYII="
  echo "$b64" | base64 -d > "$out"
}

########################################
# 1) Login (svc-core)
########################################
log "Logging in via $CORE_API/auth/login ..."
LOGIN_URL="$CORE_API/auth/login"

login_try() {
  local json="$1"
  local out="$2"
  local code
  code="$(curl_json POST "$LOGIN_URL" "$json" "" "$out")"
  if [[ "$code" == "200" || "$code" == "201" ]]; then
    local tok; tok="$(jq_first_token < "$out" || true)"
    [[ -n "$tok" && "$tok" != "null" ]] && { echo "$tok"; return 0; }
  fi
  return 1
}

TOKEN=""
if TOKEN="$(login_try "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" "$RUN_DIR/login.json")"; then
  :
elif TOKEN="$(login_try "{\"username\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" "$RUN_DIR/login.json")"; then
  :
else
  cat "$RUN_DIR/login.json" >&2 || true
  die "svc-core login failed for EMAIL=$EMAIL"
fi
echo "$TOKEN" > "$RUN_DIR/token.txt"
log "Login OK"

########################################
# 2) Create music project (svc-music) — FIXED (uses openapi enums)
########################################
MUSIC_SPEC="$RUN_DIR/music_openapi.json"
fetch_openapi "$MUSIC_BASE" "$MUSIC_SPEC"

MODE_VAL="$(enum_default_for_prop "$MUSIC_SPEC" "/api/music/projects" "post" "mode" "autopilot")"
DUET_LAYOUT_VAL="$(enum_default_for_prop "$MUSIC_SPEC" "/api/music/projects" "post" "duet_layout" "split_screen")"

PROJECT_TITLE="E2E Music Project $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
CREATE_PROJECT_URL="$MUSIC_API/music/projects"
CREATE_PROJECT_JSON="$(jq -nc \
  --arg title "$PROJECT_TITLE" \
  --arg mode "$MODE_VAL" \
  --arg duet_layout "$DUET_LAYOUT_VAL" \
  --arg language_hint "en" \
  '{title:$title, mode:$mode, duet_layout:$duet_layout, language_hint:$language_hint}'
)"

log "Creating music project (mode=$MODE_VAL duet_layout=$DUET_LAYOUT_VAL)..."
code="$(curl_json POST "$CREATE_PROJECT_URL" "$CREATE_PROJECT_JSON" "$TOKEN" "$RUN_DIR/create_project.json")"
[[ "$code" == "200" || "$code" == "201" ]] || { cat "$RUN_DIR/create_project.json" >&2; die "create project failed HTTP=$code"; }

PID="$(jq -r '.project_id // empty' "$RUN_DIR/create_project.json")"
[[ -n "$PID" && "$PID" != "null" ]] || die "could not parse project_id"
log "PID=$PID"

########################################
# 3) Upsert lyrics
########################################
LYRICS_TEXT="${LYRICS_TEXT:-$'Verse 1:\nIn the city lights, we chase the sound,\nHearts in rhythm, feet on the ground.\n\nChorus:\nDesiFaces, sing it loud,\nDreams in color, feel the crowd.'}"
LYRICS_URL="$MUSIC_API/music/projects/$PID/lyrics"
LYRICS_JSON="$(jq -nc --arg t "$LYRICS_TEXT" '{lyrics_text:$t}')"

log "Upserting lyrics..."
code="$(curl_json POST "$LYRICS_URL" "$LYRICS_JSON" "$TOKEN" "$RUN_DIR/lyrics.json")"
[[ "$code" == "200" || "$code" == "201" ]] || { cat "$RUN_DIR/lyrics.json" >&2; die "lyrics upsert failed HTTP=$code"; }

########################################
# 4) Upload voice reference (generated WAV)
########################################
VOICE_WAV="$RUN_DIR/voice_ref.wav"
python3 - <<PY
import wave, struct, math
out="${VOICE_WAV}"
fr=16000
secs=1.0
freq=440.0
n=int(fr*secs)
w=wave.open(out,'wb')
w.setnchannels(1)
w.setsampwidth(2)
w.setframerate(fr)
for i in range(n):
    s=int(0.2*32767*math.sin(2*math.pi*freq*i/fr))
    w.writeframes(struct.pack('<h', s))
w.close()
print(out)
PY

VOICE_URL="$MUSIC_API/music/projects/$PID/voice-reference"
log "Uploading voice reference..."
http_code="$(curl -sS -o "$RUN_DIR/voice_upload.json" -w "%{http_code}" \
  -X POST "$VOICE_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@$VOICE_WAV;type=audio/wav;filename=voice_ref.wav" \
  --connect-timeout 5 --max-time 60 || true)"
[[ "$http_code" == "200" || "$http_code" == "201" ]] || { cat "$RUN_DIR/voice_upload.json" >&2; die "voice upload failed HTTP=$http_code"; }

VOICE_REF_ASSET_ID="$(jq -r '.voice_ref_asset_id // empty' "$RUN_DIR/voice_upload.json")"
[[ -n "$VOICE_REF_ASSET_ID" && "$VOICE_REF_ASSET_ID" != "null" ]] || die "could not parse voice_ref_asset_id"
log "VOICE_REF_ASSET_ID=$VOICE_REF_ASSET_ID"

########################################
# 5) Trigger generate (svc-music)
########################################
GEN_URL="$MUSIC_API/music/projects/$PID/generate"
BASE_GEN_PAYLOAD='{}'
[[ -n "$INTENT" ]] && BASE_GEN_PAYLOAD="$(jq -nc --arg intent "$INTENT" '{intent:$intent}')"

log "Triggering svc-music generate: POST $GEN_URL"
post_with_autofill "$GEN_URL" "$BASE_GEN_PAYLOAD" "$TOKEN" "$RUN_DIR/generate.json" || die "generate failed (see $RUN_DIR/generate.json)"
JOB_ID="$(jq -r '.job_id // .id // .data.job_id // .data.id // empty' "$RUN_DIR/generate.json")"
[[ -n "$JOB_ID" && "$JOB_ID" != "null" ]] || { cat "$RUN_DIR/generate.json" >&2; die "could not parse job_id from generate response"; }
log "MUSIC_JOB_ID=$JOB_ID"

########################################
# 6) Poll generate job status
########################################
log "Polling svc-music job status..."
poll_music_job "$JOB_ID" || die "music job failed"

########################################
# 7) Publish job (svc-music)
########################################
PUB_URL="$MUSIC_API/music/jobs/$JOB_ID/publish"
log "Publishing svc-music job: POST $PUB_URL"
post_with_autofill "$PUB_URL" '{}' "$TOKEN" "$RUN_DIR/publish.json" || die "publish failed (see $RUN_DIR/publish.json)"

log "Polling svc-music job status post-publish..."
poll_music_job "$JOB_ID" || die "music publish failed"

########################################
# 8) Resolve audio URL (svc-music assets)
########################################
AUDIO_ARTIFACT_ID="$(extract_audio_artifact_id "$RUN_DIR/music_job_status.json" || true)"
AUDIO_URL=""
if [[ -n "${AUDIO_ARTIFACT_ID:-}" && "$AUDIO_ARTIFACT_ID" != "null" ]]; then
  log "AUDIO_ARTIFACT_ID=$AUDIO_ARTIFACT_ID"
  code="$(curl_get "$MUSIC_API/music/assets/$AUDIO_ARTIFACT_ID" "$TOKEN" "$RUN_DIR/audio_asset.json")"
  [[ "$code" == "200" ]] && AUDIO_URL="$(jq -r '.storage_ref? // .url? // .sas_url? // .data.storage_ref? // empty' "$RUN_DIR/audio_asset.json" 2>/dev/null || true)"
fi
log "AUDIO_URL=${AUDIO_URL:-<empty>}"

########################################
# 9) Placeholder image URL via svc-music assets/upload
########################################
PNG="$RUN_DIR/poster.png"
make_1x1_png "$PNG"

UPLOAD_URL="$MUSIC_API/music/assets/upload"
log "Uploading placeholder image to svc-music assets: $UPLOAD_URL"
img_code="$(curl -sS -o "$RUN_DIR/image_upload.json" -w "%{http_code}" \
  -X POST "$UPLOAD_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@$PNG;type=image/png;filename=poster.png" \
  --connect-timeout 5 --max-time 60 || true)"

IMAGE_URL=""
if [[ "$img_code" == "200" || "$img_code" == "201" ]]; then
  IMAGE_ARTIFACT_ID="$(jq -r '.artifact_id? // .id? // .data.artifact_id? // .data.id? // empty' "$RUN_DIR/image_upload.json" 2>/dev/null || true)"
  if [[ -n "$IMAGE_ARTIFACT_ID" && "$IMAGE_ARTIFACT_ID" != "null" ]]; then
    code="$(curl_get "$MUSIC_API/music/assets/$IMAGE_ARTIFACT_ID" "$TOKEN" "$RUN_DIR/image_asset.json")"
    [[ "$code" == "200" ]] && IMAGE_URL="$(jq -r '.storage_ref? // .url? // .sas_url? // .data.storage_ref? // empty' "$RUN_DIR/image_asset.json" 2>/dev/null || true)"
  fi
else
  log "WARN: image upload failed HTTP=$img_code"
  cat "$RUN_DIR/image_upload.json" >&2 || true
fi
log "IMAGE_URL=${IMAGE_URL:-<empty>}"

########################################
# 10) Attempt video sync via svc-fusion /jobs
########################################
log "Attempting fusion job via POST $FUSION_JOBS_URL"
FUSION_BASE_PAYLOAD="$(jq -nc \
  --arg pid "$PID" \
  --arg audio "${AUDIO_URL:-}" \
  --arg image "${IMAGE_URL:-}" \
  '{
    music_project_id: $pid,
    project_id: $pid,
    audio_url: $audio,
    image_url: $image
  }')"

post_with_autofill "$FUSION_JOBS_URL" "$FUSION_BASE_PAYLOAD" "$TOKEN" "$RUN_DIR/fusion_create.json" || {
  log "❌ Fusion job create failed. Likely /jobs is not meant for music-video yet."
  log "See:"
  log "  $RUN_DIR/fusion_create.json"
  log "  $RUN_DIR/fusion_create.payload_used.json"
  die "fusion video sync step failed"
}

FUSION_JOB_ID="$(jq -r '.job_id // .id // .data.job_id // .data.id // empty' "$RUN_DIR/fusion_create.json")"
[[ -n "$FUSION_JOB_ID" && "$FUSION_JOB_ID" != "null" ]] || {
  log "WARN: fusion create response had no obvious job_id; skipping polling."
  exit 0
}
log "FUSION_JOB_ID=$FUSION_JOB_ID"

FUSION_STATUS_URL="$FUSION_BASE/jobs/$FUSION_JOB_ID"
log "Polling fusion status: GET $FUSION_STATUS_URL"
for i in $(seq 1 "$POLL_MAX_TRIES"); do
  code="$(curl_get "$FUSION_STATUS_URL" "$TOKEN" "$RUN_DIR/fusion_status.json")"
  if [[ "$code" == "200" ]]; then
    st="$(jq -r '.status? // .job_status? // .state? // .data.status? // empty' "$RUN_DIR/fusion_status.json" 2>/dev/null || true)"
    [[ -z "$st" || "$st" == "null" ]] && st="unknown"
    log "fusion status ($i/$POLL_MAX_TRIES): $st"
    echo "$st" | grep -Eqi 'succeeded|completed|done|ready|success' && break
    echo "$st" | grep -Eqi 'failed|error|canceled|cancelled' && { cat "$RUN_DIR/fusion_status.json" >&2; exit 3; }
  else
    log "WARN fusion status poll HTTP=$code"
  fi
  sleep "$POLL_SECONDS"
done

log "DONE ✅"
log "PID=$PID"
log "MUSIC_JOB_ID=$JOB_ID"
log "VOICE_REF_ASSET_ID=$VOICE_REF_ASSET_ID"
log "RUN_DIR=$RUN_DIR"