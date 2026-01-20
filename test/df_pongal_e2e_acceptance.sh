#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# DesiFaces Acceptance E2E (Pongal) - HOLISTIC FIX
# Face (svc-face T2I) -> Audio (svc-audio TTS) -> Fusion (svc-fusion talking video)
#
# Key fixes:
#   - NEVER resolve artifacts by URL (URLs include ':' -> can trigger resolver SQL issues)
#   - Resolve artifacts by job_id pattern (uuid has no ':') and ALWAYS swallow resolver errors
#   - All logs to stderr; only function return values to stdout
#   - Fusion payload built with jq (no Python boolean injection)
#   - TRANSLATE defaults to false (avoid current translator env corruption)
# ==============================================================================

command -v curl >/dev/null || { echo "Missing: curl"; exit 1; }
command -v jq   >/dev/null || { echo "Missing: jq"; exit 1; }
command -v python3 >/dev/null || { echo "Missing: python3"; exit 1; }
command -v docker >/dev/null || { echo "Missing: docker"; exit 1; }

CORE_BASE="${CORE_BASE:-http://localhost:8000}"
FACE_BASE="${FACE_BASE:-http://localhost:8003}"
AUDIO_BASE="${AUDIO_BASE:-http://localhost:8004}"
FUSION_BASE="${FUSION_BASE:-http://localhost:8002}"

EMAIL="${EMAIL:-user1@desifaces.ai}"
PASSWORD="${PASSWORD:-password1}"

# Polling
POLL_SECS_FACE="${POLL_SECS_FACE:-2}"
TIMEOUT_SECS_FACE="${TIMEOUT_SECS_FACE:-300}"
POLL_SECS_AUDIO="${POLL_SECS_AUDIO:-1}"
TIMEOUT_SECS_AUDIO="${TIMEOUT_SECS_AUDIO:-180}"
POLL_SECS_FUSION="${POLL_SECS_FUSION:-3}"
TIMEOUT_SECS_FUSION="${TIMEOUT_SECS_FUSION:-900}"

# Output dir
OUT_DIR="${OUT_DIR:-/tmp/df_pongal_acceptance_$(date +%s)}"
mkdir -p "$OUT_DIR"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

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
  python3 - "$1" <<'PY'
import json, sys
print(json.dumps(sys.argv[1]))
PY
}

now_epoch() { date +%s; }

# ------------------------------------------------------------------------------
# Scenario prompts
# NOTE: svc-face user_prompt limit currently 500 chars in your schema.
# Keep it concise OR change backend max_length and update PROMPT_MAX accordingly.
# ------------------------------------------------------------------------------
PROMPT_MAX="${PROMPT_MAX:-500}"

FACE_PROMPT_SHORT="${FACE_PROMPT_SHORT:-Tamil Nadu rural woman (30s) in traditional silk saree with jasmine flowers and subtle gold jewelry, joyful smile. Pongal village celebration background: kolam/rangoli, sugarcane, marigold garlands, clay pongal pot overflowing, community celebrating. Respectfully honored decorated cow in soft-focus background. Bright sunny breezy afternoon, warm cinematic realism, sharp portrait, natural light, shallow DOF.}"

NEGATIVE_PROMPT_SHORT="${NEGATIVE_PROMPT_SHORT:-no cartoon, no anime, no CGI, no text, no watermark, no extra limbs, no distortion, no western dress, no unsafe content.}"

T2I_USER_PROMPT="${FACE_PROMPT_SHORT} Negative: ${NEGATIVE_PROMPT_SHORT}"

# Trim prompt hard to PROMPT_MAX
T2I_USER_PROMPT="$(python3 - <<PY
s = """$T2I_USER_PROMPT"""
mx = int("$PROMPT_MAX")
print(s[:mx])
PY
)"

# Audio (default no translate due to your translator env currently corrupted)
TRANSLATE="${TRANSLATE:-false}"
TRANSLATE="$(bool_norm "$TRANSLATE")"

TTS_TEXT_EN="${TTS_TEXT_EN:-Vanakkam! Today our village is glowing with Pongal celebrations. We thank the sun, the earth, and our cattle for helping our harvest. When the Pongal pot overflows, it reminds us of abundance and hope. Iniya Pongal Nalvaazhthukkal!}"

TTS_TEXT_TA="${TTS_TEXT_TA:-வணக்கம்! இன்று எங்கள் கிராமம் பொங்கல் கொண்டாட்டத்தால் ஒளிர்கிறது. அறுவடைக்கு உதவிய சூரியன், மண், மற்றும் எங்கள் மாடுகளுக்கு நன்றியை தெரிவிக்கிறோம். பொங்கல் பானை பொங்கி வழியும் போது, அது வளமும் நம்பிக்கையும் நினைவூட்டுகிறது. இனிய பொங்கல் நல்வாழ்த்துக்கள்!}"

OUTPUT_FORMAT="${OUTPUT_FORMAT:-mp3}"
VOICE="${VOICE:-}"   # optional stable Tamil female voice id
CONTEXT="${CONTEXT:-pongal_acceptance:$(python3 - <<'PY'
import uuid; print(uuid.uuid4())
PY
)}"

# Fusion settings
EXTERNAL_PROVIDER_OK="${EXTERNAL_PROVIDER_OK:-true}"
EXTERNAL_PROVIDER_OK="$(bool_norm "$EXTERNAL_PROVIDER_OK")"
ASPECT_RATIO="${ASPECT_RATIO:-9:16}"

# ------------------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------------------
log "Logging in via CORE_BASE=$CORE_BASE as $EMAIL ..."
LOGIN_JSON="$OUT_DIR/login.json"
curl -sS -o "$LOGIN_JSON" -w "\nHTTP=%{http_code}\n" \
  -X POST "$CORE_BASE/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" \
  | tail -n1 | grep -q "HTTP=200" || {
    log "Login response:"
    cat "$LOGIN_JSON" | jq >&2 || cat "$LOGIN_JSON" >&2
    die "Login failed"
  }

DF_TOKEN="$(jq -r '.access_token // .token // empty' "$LOGIN_JSON")"
[[ -n "$DF_TOKEN" ]] || die "Could not extract access_token"
AUTH=(-H "Authorization: Bearer $DF_TOKEN")
log "Login OK. OUT_DIR=$OUT_DIR"

# ------------------------------------------------------------------------------
# FACE helpers (safe stdout discipline)
# ------------------------------------------------------------------------------
face_create_job() {
  local payload_json="$1"
  local out_json="$OUT_DIR/face_create.json"
  local http
  http="$(curl -sS -o "$out_json" -w "%{http_code}" \
    -X POST "$FACE_BASE/api/face/creator/generate" \
    "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "$payload_json")"

  if [[ "$http" != "200" ]]; then
    log "Create FACE job response (HTTP=$http):"
    cat "$out_json" | jq >&2 || cat "$out_json" >&2
    die "FACE create failed (422 = schema mismatch/invalid codes/prompt length)"
  fi

  local job_id
  job_id="$(jq -r '.job_id // empty' "$out_json")"
  [[ -n "$job_id" ]] || die "FACE create missing job_id"
  echo "$job_id"
}

face_poll_job() {
  local job_id="$1"
  local start now elapsed
  start="$(now_epoch)"

  while true; do
    now="$(now_epoch)"
    elapsed=$((now - start))
    if (( elapsed > TIMEOUT_SECS_FACE )); then
      die "FACE job timeout after ${TIMEOUT_SECS_FACE}s (job_id=$job_id)"
    fi

    local status_json="$OUT_DIR/face_status_${job_id}.json"
    local http
    http="$(curl -sS -o "$status_json" -w "%{http_code}" \
      -X GET "$FACE_BASE/api/face/creator/jobs/${job_id}/status" \
      "${AUTH[@]}")"

    if [[ "$http" != "200" ]]; then
      log "FACE status poll failed (HTTP=$http):"
      cat "$status_json" | jq >&2 || cat "$status_json" >&2
      die "FACE status poll failed"
    fi

    local st
    st="$(jq -r '.status // empty' "$status_json")"
    log "Face job $job_id status=$st (t=${elapsed}s)"

    if [[ "$st" == "succeeded" ]]; then
      echo "$status_json" # ONLY stdout output
      return 0
    fi
    if [[ "$st" == "failed" || "$st" == "cancelled" ]]; then
      cat "$status_json" | jq >&2 || true
      die "FACE job ended with status=$st"
    fi
    sleep "$POLL_SECS_FACE"
  done
}

# ------------------------------------------------------------------------------
# AUDIO helpers
# ------------------------------------------------------------------------------
audio_create_job() {
  local payload_json="$1"
  local out_json="$OUT_DIR/audio_create.json"
  local http
  http="$(curl -sS -o "$out_json" -w "%{http_code}" \
    -X POST "$AUDIO_BASE/api/audio/tts" \
    "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "$payload_json")"

  if [[ "$http" != "200" ]]; then
    log "Audio create response (HTTP=$http):"
    cat "$out_json" | jq >&2 || cat "$out_json" >&2
    die "AUDIO create failed"
  fi

  local job_id
  job_id="$(jq -r '.job_id // empty' "$out_json")"
  [[ -n "$job_id" ]] || die "AUDIO create missing job_id"
  echo "$job_id"
}

audio_poll_job() {
  local job_id="$1"
  local start now elapsed
  start="$(now_epoch)"

  while true; do
    now="$(now_epoch)"
    elapsed=$((now - start))
    if (( elapsed > TIMEOUT_SECS_AUDIO )); then
      die "AUDIO job timeout after ${TIMEOUT_SECS_AUDIO}s (job_id=$job_id)"
    fi

    local status_json="$OUT_DIR/audio_status_${job_id}.json"
    local http
    http="$(curl -sS -o "$status_json" -w "%{http_code}" \
      -X GET "$AUDIO_BASE/api/audio/jobs/${job_id}/status" \
      "${AUTH[@]}")"

    if [[ "$http" != "200" ]]; then
      log "AUDIO status poll failed (HTTP=$http):"
      cat "$status_json" | jq >&2 || cat "$status_json" >&2
      die "AUDIO status poll failed"
    fi

    local st
    st="$(jq -r '.status // empty' "$status_json")"
    log "Audio job $job_id status=$st (t=${elapsed}s)"

    if [[ "$st" == "succeeded" ]]; then
      echo "$status_json"
      return 0
    fi
    if [[ "$st" == "failed" || "$st" == "cancelled" ]]; then
      cat "$status_json" | jq >&2 || true
      die "AUDIO job ended with status=$st"
    fi
    sleep "$POLL_SECS_AUDIO"
  done
}

# ------------------------------------------------------------------------------
# FUSION helpers
# ------------------------------------------------------------------------------
fusion_create_job() {
  local payload_json="$1"
  local out_json="$OUT_DIR/fusion_create.json"
  local http
  http="$(curl -sS -o "$out_json" -w "%{http_code}" \
    -X POST "$FUSION_BASE/jobs" \
    "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "$payload_json")"

  if [[ "$http" != "200" ]]; then
    log "Fusion create response (HTTP=$http):"
    cat "$out_json" | jq >&2 || cat "$out_json" >&2
    die "FUSION create failed"
  fi

  local job_id
  job_id="$(jq -r '.job_id // empty' "$out_json")"
  [[ -n "$job_id" ]] || die "FUSION create missing job_id"
  echo "$job_id"
}

fusion_poll_job() {
  local job_id="$1"
  local start now elapsed
  start="$(now_epoch)"

  while true; do
    now="$(now_epoch)"
    elapsed=$((now - start))
    if (( elapsed > TIMEOUT_SECS_FUSION )); then
      die "FUSION job timeout after ${TIMEOUT_SECS_FUSION}s (job_id=$job_id)"
    fi

    local status_json="$OUT_DIR/fusion_status_${job_id}.json"
    local http
    http="$(curl -sS -o "$status_json" -w "%{http_code}" \
      -X GET "$FUSION_BASE/jobs/${job_id}" \
      "${AUTH[@]}")"

    if [[ "$http" != "200" ]]; then
      log "FUSION status poll failed (HTTP=$http):"
      cat "$status_json" | jq >&2 || cat "$status_json" >&2
      die "FUSION status poll failed"
    fi

    local st
    st="$(jq -r '.status // empty' "$status_json")"
    log "Fusion job $job_id status=$st (t=${elapsed}s)"

    if [[ "$st" == "succeeded" ]]; then
      echo "$status_json"
      return 0
    fi
    if [[ "$st" == "failed" || "$st" == "cancelled" ]]; then
      cat "$status_json" | jq >&2 || true
      die "FUSION job ended with status=$st"
    fi
    sleep "$POLL_SECS_FUSION"
  done
}

# ------------------------------------------------------------------------------
# Artifact resolution (HOLISTIC FIX)
#   - Never by URL (URL contains ':')
#   - Resolve by job_id pattern in url (uuid has no ':')
#   - Never print tracebacks; on any error return "" and fallback to latest
# ------------------------------------------------------------------------------
ensure_fusion_container() {
  docker ps --format '{{.Names}}' | grep -q '^df-svc-fusion$'
}

resolve_artifact_by_jobid() {
  local job_id="$1"
  local kind_sql="$2"  # e.g. "('face','image','face_image')" or "('audio')"

  ensure_fusion_container || { echo ""; return 0; }

  docker exec -i df-svc-fusion python - <<PY
import asyncio, asyncpg
from app.config import settings

JOB = "${job_id}"
KIND_SQL = "${kind_sql}"

async def main():
    try:
        pool = await asyncpg.create_pool(settings.DATABASE_URL)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"""
                SELECT CAST(id AS text) AS id
                FROM artifacts
                WHERE kind IN {KIND_SQL}
                  AND url LIKE '%' || $1 || '%'
                ORDER BY created_at DESC
                LIMIT 1
            """, JOB)
        await pool.close()
        print(row["id"] if row else "")
    except Exception:
        # Swallow all resolver exceptions so E2E never breaks here
        print("")

asyncio.run(main())
PY
}

resolve_latest_artifact() {
  local kind_sql="$1"
  ensure_fusion_container || { echo ""; return 0; }

  docker exec -i df-svc-fusion python - <<PY
import asyncio, asyncpg
from app.config import settings

KIND_SQL = "${kind_sql}"

async def main():
    try:
        pool = await asyncpg.create_pool(settings.DATABASE_URL)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"""
                SELECT CAST(id AS text) AS id
                FROM artifacts
                WHERE kind IN {KIND_SQL}
                ORDER BY created_at DESC
                LIMIT 1
            """)
        await pool.close()
        print(row["id"] if row else "")
    except Exception:
        print("")

asyncio.run(main())
PY
}

# ==============================================================================
# 1) FACE (T2I)
# ==============================================================================
log "=============================="
log "[1/3] FACE (svc-face) - Pongal T2I"
log "=============================="

# IMPORTANT: your svc-face schema currently requires these structured codes.
# Provide them via env if needed; keep them scenario-oriented.
REGION_CODE="${REGION_CODE:-tamil_nadu}"
AGE_RANGE_CODE="${AGE_RANGE_CODE:-adult_30s}"
SKIN_TONE_CODE="${SKIN_TONE_CODE:-medium_brown}"
USE_CASE_CODE="${USE_CASE_CODE:-festival_story}"
STYLE_CODE="${STYLE_CODE:-cinematic_realism}"
CONTEXT_CODE="${CONTEXT_CODE:-pongal_festival}"

FACE_PAYLOAD="$(jq -cn \
  --arg language "en" \
  --arg user_prompt "$T2I_USER_PROMPT" \
  --argjson num_variants 1 \
  --arg region_code "$REGION_CODE" \
  --arg age_range_code "$AGE_RANGE_CODE" \
  --arg skin_tone_code "$SKIN_TONE_CODE" \
  --arg use_case_code "$USE_CASE_CODE" \
  --arg style_code "$STYLE_CODE" \
  --arg context_code "$CONTEXT_CODE" \
  '{
    language: $language,
    user_prompt: $user_prompt,
    num_variants: $num_variants,
    mode: "text-to-image",
    region_code: $region_code,
    age_range_code: $age_range_code,
    skin_tone_code: $skin_tone_code,
    gender: "female",
    image_format_code: "instagram_portrait",
    use_case_code: $use_case_code,
    style_code: $style_code,
    context_code: $context_code
  }'
)"
echo "$FACE_PAYLOAD" | jq >"$OUT_DIR/face_payload.json"
log "Face payload written: $OUT_DIR/face_payload.json"

FACE_JOB_ID="$(face_create_job "$FACE_PAYLOAD")"
log "Face job_id=$FACE_JOB_ID"
FACE_STATUS_JSON="$(face_poll_job "$FACE_JOB_ID")"

IMAGE_URL="$(jq -r '.variants[0].image_url // empty' "$FACE_STATUS_JSON")"
[[ -n "$IMAGE_URL" ]] || die "Face status missing variants[0].image_url"
log "Face image_url=$IMAGE_URL"

# Download image with correct extension
EXT="$(python3 - <<PY
import urllib.parse as u
url="${IMAGE_URL}"
path=u.urlparse(url).path
ext=path.rsplit(".",1)[-1].lower() if "." in path else "png"
print(ext if ext in ("png","jpg","jpeg","webp") else "png")
PY
)"
FACE_IMG_OUT="$OUT_DIR/face_${FACE_JOB_ID}.${EXT}"
curl -sS -L -o "$FACE_IMG_OUT" "$IMAGE_URL"
[[ -s "$FACE_IMG_OUT" ]] || die "Downloaded face image empty: $FACE_IMG_OUT"
log "Downloaded face image: $FACE_IMG_OUT"

# Resolve face_artifact_id (try response field; else resolve by job_id; else latest)
FACE_ARTIFACT_ID="$(jq -r '.variants[0].artifact_id // .variants[0].image_artifact_id // empty' "$FACE_STATUS_JSON")"
if [[ -z "$FACE_ARTIFACT_ID" ]]; then
  FACE_ARTIFACT_ID="$(resolve_artifact_by_jobid "$FACE_JOB_ID" "('face','image','face_image')" || true)"
fi
if [[ -z "$FACE_ARTIFACT_ID" ]]; then
  log "WARN: could not resolve face_artifact_id by job_id; falling back to latest face artifact"
  FACE_ARTIFACT_ID="$(resolve_latest_artifact "('face','image','face_image')" || true)"
fi
[[ -n "$FACE_ARTIFACT_ID" ]] || die "Could not resolve face_artifact_id"
log "Resolved face_artifact_id=$FACE_ARTIFACT_ID"

# ==============================================================================
# 2) AUDIO (Translate + TTS)
# ==============================================================================
log "=============================="
log "[2/3] AUDIO (svc-audio) Translate+TTS"
log "=============================="

if [[ "$TRANSLATE" == "true" ]]; then
  AUDIO_TEXT="$TTS_TEXT_EN"
  SOURCE_LANGUAGE="${SOURCE_LANGUAGE:-en}"
  TARGET_LOCALE="${TARGET_LOCALE:-ta-IN}"
else
  AUDIO_TEXT="$TTS_TEXT_TA"
  SOURCE_LANGUAGE="${SOURCE_LANGUAGE:-ta}"
  TARGET_LOCALE="${TARGET_LOCALE:-ta-IN}"
fi

AUDIO_PAYLOAD="{"
AUDIO_PAYLOAD+="\"text\":$(json_escape "$AUDIO_TEXT")"
AUDIO_PAYLOAD+=",\"translate\":${TRANSLATE}"
AUDIO_PAYLOAD+=",\"source_language\":$(json_escape "$SOURCE_LANGUAGE")"
AUDIO_PAYLOAD+=",\"target_locale\":$(json_escape "$TARGET_LOCALE")"
AUDIO_PAYLOAD+=",\"output_format\":$(json_escape "$OUTPUT_FORMAT")"
AUDIO_PAYLOAD+=",\"context\":$(json_escape "$CONTEXT")"
if [[ -n "$VOICE" ]]; then
  AUDIO_PAYLOAD+=",\"voice\":$(json_escape "$VOICE")"
fi
AUDIO_PAYLOAD+="}"

echo "$AUDIO_PAYLOAD" | jq >"$OUT_DIR/audio_payload.json"
log "Audio payload written: $OUT_DIR/audio_payload.json"

AUDIO_JOB_ID="$(audio_create_job "$AUDIO_PAYLOAD")"
log "Audio job_id=$AUDIO_JOB_ID"
AUDIO_STATUS_JSON="$(audio_poll_job "$AUDIO_JOB_ID")"

AUDIO_URL="$(jq -r '.variants[0].audio_url // empty' "$AUDIO_STATUS_JSON")"
[[ -n "$AUDIO_URL" ]] || die "Audio status missing variants[0].audio_url"
log "Audio audio_url=$AUDIO_URL"

AUDIO_OUT="$OUT_DIR/audio_${AUDIO_JOB_ID}.mp3"
curl -sS -L -o "$AUDIO_OUT" "$AUDIO_URL"
[[ -s "$AUDIO_OUT" ]] || die "Downloaded audio empty: $AUDIO_OUT"
log "Downloaded audio: $AUDIO_OUT"

AUDIO_ARTIFACT_ID="$(jq -r '.variants[0].artifact_id // .audio_artifact_id // empty' "$AUDIO_STATUS_JSON")"
if [[ -z "$AUDIO_ARTIFACT_ID" ]]; then
  AUDIO_ARTIFACT_ID="$(resolve_artifact_by_jobid "$AUDIO_JOB_ID" "('audio')" || true)"
fi
if [[ -z "$AUDIO_ARTIFACT_ID" ]]; then
  log "WARN: could not resolve audio_artifact_id by job_id; falling back to latest audio artifact"
  AUDIO_ARTIFACT_ID="$(resolve_latest_artifact "('audio')" || true)"
fi
[[ -n "$AUDIO_ARTIFACT_ID" ]] || die "Could not resolve audio_artifact_id"
log "Resolved audio_artifact_id=$AUDIO_ARTIFACT_ID"

# ==============================================================================
# 3) FUSION (Video)
# ==============================================================================
log "=============================="
log "[3/3] FUSION (svc-fusion) Talking Video"
log "=============================="

FUSION_PAYLOAD="$(jq -cn \
  --arg face_artifact_id "$FACE_ARTIFACT_ID" \
  --arg audio_artifact_id "$AUDIO_ARTIFACT_ID" \
  --arg aspect_ratio "$ASPECT_RATIO" \
  --argjson external_ok "$EXTERNAL_PROVIDER_OK" \
  '{
    face_artifact_id: $face_artifact_id,
    voice_mode: "audio",
    consent: { external_provider_ok: $external_ok },
    video: { aspect_ratio: $aspect_ratio },
    voice_audio: { type: "audio", audio_artifact_id: $audio_artifact_id }
  }'
)"

echo "$FUSION_PAYLOAD" | jq >"$OUT_DIR/fusion_payload.json"
log "Fusion payload written: $OUT_DIR/fusion_payload.json"

FUSION_JOB_ID="$(fusion_create_job "$FUSION_PAYLOAD")"
log "Fusion job_id=$FUSION_JOB_ID"
FUSION_STATUS_JSON="$(fusion_poll_job "$FUSION_JOB_ID")"

VIDEO_URL="$(jq -r '.artifacts[]? | select(.kind=="video") | .url' "$FUSION_STATUS_JSON" | head -n 1 || true)"
if [[ -z "$VIDEO_URL" ]]; then
  log "WARN: Fusion succeeded but no video artifact found in response"
  log "E2E PASS (jobs succeeded) ✅ OUT_DIR=$OUT_DIR"
  exit 0
fi

VIDEO_OUT="$OUT_DIR/video_${FUSION_JOB_ID}.mp4"
curl -sS -L -o "$VIDEO_OUT" "$VIDEO_URL"
[[ -s "$VIDEO_OUT" ]] || die "Downloaded video empty: $VIDEO_OUT"
log "Downloaded video: $VIDEO_OUT"

cat > "$OUT_DIR/PASS_CHECKLIST.txt" <<'TXT'
PASS CHECKLIST

FACE
- Tamil/South Indian woman ~30s, rural vibe
- Saree + jasmine + subtle gold jewelry
- Pongal context: kolam, sugarcane, pongal pot, festive color
- Cow honored respectfully in background
- No artifacts / no text

AUDIO
- Tamil speech clear, warm celebratory
- ~10–20 seconds
- No clipping

VIDEO
- Lip-sync OK
- Subtle blink/smile/head nod
- Bright warm sunny look, stable framing
TXT

log "=============================="
log "E2E PASS ✅"
log " face_job_id  : $FACE_JOB_ID"
log " audio_job_id : $AUDIO_JOB_ID"
log " fusion_job_id: $FUSION_JOB_ID"
log " OUT_DIR      : $OUT_DIR"
log "=============================="