#!/usr/bin/env bash
set -Eeuo pipefail

# E2E smoke test for svc-fusion -> Hedra using face/audio artifacts pulled from Postgres.
#
# Defaults:
#   DF_EMAIL=user1@desifaces.ai
#   DF_PASSWORD=password1
#   CORE_URL=http://localhost:8000
#   FUSION_URL=http://localhost:8002
#   DB_CONTAINER=desifaces-db
#
# Behavior:
#   1) Logs in via svc-core
#   2) Resolves the user's UUID from DB
#   3) Pulls the latest usable face artifact id + audio artifact id from DB
#   4) Calls /jobs/pricing/preview (best effort)
#   5) Creates a Hedra fusion job using artifact ids (not raw URLs)
#   6) Polls until terminal status and prints final artifacts / errors

SCRIPT_NAME="$(basename "$0")"
OUT_DIR="${OUT_DIR:-/tmp/${SCRIPT_NAME%.sh}_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

CORE_URL="${CORE_URL:-http://localhost:8000}"
FUSION_URL="${FUSION_URL:-http://localhost:8002}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
DF_EMAIL="${DF_EMAIL:-user1@desifaces.ai}"
DF_PASSWORD="${DF_PASSWORD:-password1}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLL_SECONDS="${POLL_SECONDS:-5}"
VIDEO_DURATION_SEC="${VIDEO_DURATION_SEC:-6}"
VIDEO_ASPECT_RATIO="${VIDEO_ASPECT_RATIO:-9:16}"
VIDEO_RESOLUTION="${VIDEO_RESOLUTION:-720p}"
HEDRA_MODEL_NAME="${HEDRA_MODEL_NAME:-Hedra Omnia}"
HEDRA_PROMPT="${HEDRA_PROMPT:-Warm reverent storytelling, subtle upper-body movement, expressive face, natural blinking, premium cinematic realism.}"
ALLOW_ANY_USER_ARTIFACTS="${ALLOW_ANY_USER_ARTIFACTS:-0}"
SKIP_PRICING_PREVIEW="${SKIP_PRICING_PREVIEW:-0}"
EXTERNAL_PROVIDER_OK="${EXTERNAL_PROVIDER_OK:-true}"

ACCESS_TOKEN=""
AUTH_USER_ID=""
DB_USER_ID=""
FACE_ARTIFACT_ID="${FACE_ARTIFACT_ID:-}"
AUDIO_ARTIFACT_ID="${AUDIO_ARTIFACT_ID:-}"
JOB_ID=""

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

sql_escape() {
  printf "%s" "$1" | sed "s/'/''/g"
}

psql_exec() {
  local sql="$1"
  docker exec -i "$DB_CONTAINER" bash -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-postgres}" -Atq' <<<"$sql"
}

try_psql_scalar() {
  local sql="$1"
  if out=$(psql_exec "$sql" 2>/dev/null); then
    printf "%s" "$out" | tr -d '\r' | awk 'NF {print; exit}'
    return 0
  fi
  return 1
}

json_get() {
  local file="$1"
  local expr="$2"
  jq -r "$expr // empty" "$file"
}

decode_jwt_sub() {
  python3 - "$1" <<'PY'
import base64, json, sys

token = sys.argv[1].strip()
parts = token.split('.')
if len(parts) < 2:
    print('')
    raise SystemExit(0)
payload = parts[1]
payload += '=' * (-len(payload) % 4)
try:
    data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
except Exception:
    print('')
    raise SystemExit(0)
print(data.get('sub', '') or '')
PY
}

http_post_json() {
  local url="$1"
  local payload_file="$2"
  local out_file="$3"
  shift 3
  curl -sS -o "$out_file" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    "$@" \
    --data @"$payload_file" \
    "$url"
}

http_get_json() {
  local url="$1"
  local out_file="$2"
  shift 2
  curl -sS -o "$out_file" -w '%{http_code}' "$@" "$url"
}

pick_user_id_from_db() {
  local email_esc
  email_esc="$(sql_escape "$DF_EMAIL")"

  local q1="SELECT id::text FROM core.users WHERE lower(email)=lower('${email_esc}') ORDER BY created_at DESC NULLS LAST LIMIT 1;"
  local q2="SELECT id::text FROM users WHERE lower(email)=lower('${email_esc}') ORDER BY created_at DESC NULLS LAST LIMIT 1;"

  DB_USER_ID="$(try_psql_scalar "$q1" || true)"
  if [[ -z "$DB_USER_ID" ]]; then
    DB_USER_ID="$(try_psql_scalar "$q2" || true)"
  fi

  [[ -n "$DB_USER_ID" ]] || fail "Could not resolve user id from DB for ${DF_EMAIL}"
}

pick_face_artifact_id() {
  local uid="$1"
  local q_join="
SELECT a.id::text
FROM artifacts a
JOIN studio_jobs j ON j.id = a.job_id
WHERE j.user_id = '${uid}'::uuid
  AND COALESCE(j.status, '') = 'succeeded'
  AND COALESCE(a.url, '') <> ''
  AND (
    lower(COALESCE(a.kind, '')) IN ('face', 'face_image', 'image')
    OR lower(COALESCE(a.content_type, '')) LIKE 'image/%'
  )
ORDER BY
  CASE
    WHEN lower(COALESCE(a.kind, '')) = 'face' THEN 0
    WHEN lower(COALESCE(a.kind, '')) = 'face_image' THEN 1
    WHEN lower(COALESCE(a.kind, '')) = 'image' THEN 2
    ELSE 9
  END,
  a.created_at DESC NULLS LAST
LIMIT 1;"

  local q_any_user="
SELECT a.id::text
FROM artifacts a
WHERE COALESCE(a.url, '') <> ''
  AND (
    lower(COALESCE(a.kind, '')) IN ('face', 'face_image', 'image')
    OR lower(COALESCE(a.content_type, '')) LIKE 'image/%'
  )
ORDER BY
  CASE
    WHEN lower(COALESCE(a.kind, '')) = 'face' THEN 0
    WHEN lower(COALESCE(a.kind, '')) = 'face_image' THEN 1
    WHEN lower(COALESCE(a.kind, '')) = 'image' THEN 2
    ELSE 9
  END,
  a.created_at DESC NULLS LAST
LIMIT 1;"

  FACE_ARTIFACT_ID="$(try_psql_scalar "$q_join" || true)"
  if [[ -z "$FACE_ARTIFACT_ID" && "$ALLOW_ANY_USER_ARTIFACTS" == "1" ]]; then
    FACE_ARTIFACT_ID="$(try_psql_scalar "$q_any_user" || true)"
  fi

  [[ -n "$FACE_ARTIFACT_ID" ]] || fail "Could not find a usable face/image artifact id in DB"
}

pick_audio_artifact_id() {
  local uid="$1"
  local q_join="
SELECT a.id::text
FROM artifacts a
JOIN studio_jobs j ON j.id = a.job_id
WHERE j.user_id = '${uid}'::uuid
  AND COALESCE(j.status, '') = 'succeeded'
  AND COALESCE(a.url, '') <> ''
  AND (
    lower(COALESCE(a.kind, '')) IN ('audio', 'voice_audio', 'tts_audio', 'audio_master', 'full_mix')
    OR lower(COALESCE(a.content_type, '')) LIKE 'audio/%'
  )
ORDER BY
  CASE
    WHEN lower(COALESCE(a.kind, '')) = 'audio' THEN 0
    WHEN lower(COALESCE(a.kind, '')) = 'voice_audio' THEN 1
    WHEN lower(COALESCE(a.kind, '')) = 'tts_audio' THEN 2
    WHEN lower(COALESCE(a.kind, '')) = 'audio_master' THEN 3
    WHEN lower(COALESCE(a.kind, '')) = 'full_mix' THEN 4
    ELSE 9
  END,
  a.created_at DESC NULLS LAST
LIMIT 1;"

  local q_any_user="
SELECT a.id::text
FROM artifacts a
WHERE COALESCE(a.url, '') <> ''
  AND (
    lower(COALESCE(a.kind, '')) IN ('audio', 'voice_audio', 'tts_audio', 'audio_master', 'full_mix')
    OR lower(COALESCE(a.content_type, '')) LIKE 'audio/%'
  )
ORDER BY
  CASE
    WHEN lower(COALESCE(a.kind, '')) = 'audio' THEN 0
    WHEN lower(COALESCE(a.kind, '')) = 'voice_audio' THEN 1
    WHEN lower(COALESCE(a.kind, '')) = 'tts_audio' THEN 2
    WHEN lower(COALESCE(a.kind, '')) = 'audio_master' THEN 3
    WHEN lower(COALESCE(a.kind, '')) = 'full_mix' THEN 4
    ELSE 9
  END,
  a.created_at DESC NULLS LAST
LIMIT 1;"

  AUDIO_ARTIFACT_ID="$(try_psql_scalar "$q_join" || true)"
  if [[ -z "$AUDIO_ARTIFACT_ID" && "$ALLOW_ANY_USER_ARTIFACTS" == "1" ]]; then
    AUDIO_ARTIFACT_ID="$(try_psql_scalar "$q_any_user" || true)"
  fi

  [[ -n "$AUDIO_ARTIFACT_ID" ]] || fail "Could not find a usable audio artifact id in DB"
}

show_artifact_debug() {
  local artifact_id="$1"
  local label="$2"
  local q="SELECT json_build_object(
    'id', a.id::text,
    'kind', a.kind,
    'content_type', a.content_type,
    'url', a.url,
    'job_id', a.job_id::text,
    'created_at', a.created_at
  )::text
  FROM artifacts a
  WHERE a.id = '${artifact_id}'::uuid
  LIMIT 1;"
  local out
  out="$(try_psql_scalar "$q" || true)"
  if [[ -n "$out" ]]; then
    printf '%s\n' "$out" > "$OUT_DIR/${label}_artifact.json"
  fi
}

login() {
  cat > "$OUT_DIR/login_request.json" <<JSON
{"email":"$DF_EMAIL","password":"$DF_PASSWORD"}
JSON

  local code
  code="$(http_post_json "$CORE_URL/api/auth/login" "$OUT_DIR/login_request.json" "$OUT_DIR/login_response.json")"
  [[ "$code" == "200" ]] || fail "Login failed (HTTP ${code}). See $OUT_DIR/login_response.json"

  ACCESS_TOKEN="$(json_get "$OUT_DIR/login_response.json" '.access_token')"
  [[ -n "$ACCESS_TOKEN" ]] || fail "Login succeeded but access_token missing"

  AUTH_USER_ID="$(json_get "$OUT_DIR/login_response.json" '.user.id')"
  if [[ -z "$AUTH_USER_ID" ]]; then
    AUTH_USER_ID="$(json_get "$OUT_DIR/login_response.json" '.user_id')"
  fi
  if [[ -z "$AUTH_USER_ID" ]]; then
    AUTH_USER_ID="$(decode_jwt_sub "$ACCESS_TOKEN")"
  fi

  [[ -n "$AUTH_USER_ID" ]] || fail "Could not derive authenticated user id from login response/JWT"
}

build_payload() {
  FACE_ARTIFACT_ID="$FACE_ARTIFACT_ID" \
  AUDIO_ARTIFACT_ID="$AUDIO_ARTIFACT_ID" \
  VIDEO_ASPECT_RATIO="$VIDEO_ASPECT_RATIO" \
  VIDEO_RESOLUTION="$VIDEO_RESOLUTION" \
  VIDEO_DURATION_SEC="$VIDEO_DURATION_SEC" \
  HEDRA_MODEL_NAME="$HEDRA_MODEL_NAME" \
  HEDRA_PROMPT="$HEDRA_PROMPT" \
  EXTERNAL_PROVIDER_OK="$EXTERNAL_PROVIDER_OK" \
  DF_EMAIL="$DF_EMAIL" \
  python3 - <<'PY' > "$OUT_DIR/fusion_create_payload.json"
import json
import os

payload = {
    "provider": "hedra",
    "face_artifact_id": os.environ["FACE_ARTIFACT_ID"],
    "voice_mode": "audio",
    "voice_audio": {
        "type": "audio",
        "audio_artifact_id": os.environ["AUDIO_ARTIFACT_ID"],
    },
    "video": {
        "aspect_ratio": os.environ["VIDEO_ASPECT_RATIO"],
        "resolution": os.environ["VIDEO_RESOLUTION"],
        "duration_sec": int(float(os.environ["VIDEO_DURATION_SEC"])),
        "shot_type": "portrait",
    },
    "provider_options": {
        "model_name": os.environ["HEDRA_MODEL_NAME"],
        "shot_type": "portrait",
        "prompt": os.environ["HEDRA_PROMPT"],
    },
    "consent": {
        "external_provider_ok": str(os.environ.get("EXTERNAL_PROVIDER_OK", "true")).strip().lower() in {"1", "true", "yes", "y"},
    },
    "tags": {
        "source": "df_e2e_fusion_hedra_from_db",
        "email": os.environ["DF_EMAIL"],
    },
}
print(json.dumps(payload, separators=(",", ":")))
PY
}

run_preview() {
  if [[ "$SKIP_PRICING_PREVIEW" == "1" ]]; then
    log "Skipping pricing preview because SKIP_PRICING_PREVIEW=1"
    return 0
  fi

  local code
  code="$(http_post_json "$FUSION_URL/jobs/pricing/preview" "$OUT_DIR/fusion_create_payload.json" "$OUT_DIR/pricing_preview_response.json" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "X-User-Id: $AUTH_USER_ID")"

  if [[ "$code" =~ ^2 ]]; then
    log "Pricing preview succeeded"
    jq '.' "$OUT_DIR/pricing_preview_response.json" > "$OUT_DIR/pricing_preview_pretty.json" || true
    return 0
  fi

  log "Pricing preview returned HTTP $code; continuing to create job"
  cp "$OUT_DIR/pricing_preview_response.json" "$OUT_DIR/pricing_preview_error.json" || true
  return 0
}

create_job() {
  local code
  code="$(http_post_json "$FUSION_URL/jobs" "$OUT_DIR/fusion_create_payload.json" "$OUT_DIR/create_job_response.json" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    -H "X-User-Id: $AUTH_USER_ID")"

  [[ "$code" =~ ^2 ]] || fail "Create job failed (HTTP ${code}). See $OUT_DIR/create_job_response.json"

  JOB_ID="$(json_get "$OUT_DIR/create_job_response.json" '.job_id')"
  [[ -n "$JOB_ID" ]] || fail "Create job succeeded but job_id missing"
}

poll_job() {
  local start_ts now elapsed code status
  start_ts="$(date +%s)"

  while true; do
    code="$(http_get_json "$FUSION_URL/jobs/$JOB_ID/status" "$OUT_DIR/status_latest.json" \
      -H "Authorization: Bearer $ACCESS_TOKEN" \
      -H "X-User-Id: $AUTH_USER_ID")"

    [[ "$code" =~ ^2 ]] || fail "Status poll failed (HTTP ${code}). See $OUT_DIR/status_latest.json"

    status="$(json_get "$OUT_DIR/status_latest.json" '.status')"
    now="$(date +%s)"
    elapsed=$(( now - start_ts ))

    cp "$OUT_DIR/status_latest.json" "$OUT_DIR/status_${elapsed}s.json"

    printf '%s\n' "elapsed=${elapsed}s status=${status:-unknown}"

    if [[ "$status" == "succeeded" || "$status" == "failed" || "$status" == "blocked" || "$status" == "canceled" ]]; then
      break
    fi

    if (( elapsed >= TIMEOUT_SECONDS )); then
      fail "Timed out after ${TIMEOUT_SECONDS}s waiting for terminal status"
    fi

    sleep "$POLL_SECONDS"
  done
}

print_summary() {
  local final_status provider provider_job_id error_code error_message video_url share_url
  final_status="$(json_get "$OUT_DIR/status_latest.json" '.status')"
  provider="$(json_get "$OUT_DIR/status_latest.json" '.provider')"
  provider_job_id="$(json_get "$OUT_DIR/status_latest.json" '.provider_job_id')"
  error_code="$(json_get "$OUT_DIR/status_latest.json" '.error_code')"
  error_message="$(json_get "$OUT_DIR/status_latest.json" '.error_message')"
  video_url="$(jq -r '.artifacts[]? | select(.kind=="video") | .url' "$OUT_DIR/status_latest.json" | head -n1)"
  share_url="$(jq -r '.artifacts[]? | select(.kind=="share_url") | .url' "$OUT_DIR/status_latest.json" | head -n1)"

  jq '.' "$OUT_DIR/create_job_response.json" > "$OUT_DIR/create_job_response_pretty.json" || true
  jq '.' "$OUT_DIR/status_latest.json" > "$OUT_DIR/status_latest_pretty.json" || true

  cat > "$OUT_DIR/summary.txt" <<EOF
OUT_DIR=$OUT_DIR
CORE_URL=$CORE_URL
FUSION_URL=$FUSION_URL
DB_CONTAINER=$DB_CONTAINER
DF_EMAIL=$DF_EMAIL
AUTH_USER_ID=$AUTH_USER_ID
DB_USER_ID=$DB_USER_ID
FACE_ARTIFACT_ID=$FACE_ARTIFACT_ID
AUDIO_ARTIFACT_ID=$AUDIO_ARTIFACT_ID
JOB_ID=$JOB_ID
PROVIDER=$provider
PROVIDER_JOB_ID=$provider_job_id
FINAL_STATUS=$final_status
ERROR_CODE=$error_code
ERROR_MESSAGE=$error_message
VIDEO_URL=$video_url
SHARE_URL=$share_url
EOF

  cat "$OUT_DIR/summary.txt"

  echo
  echo "Artifacts:"
  jq -r '.artifacts // []' "$OUT_DIR/status_latest.json" || true

  echo
  echo "Steps:"
  jq -r '.steps // []' "$OUT_DIR/status_latest.json" || true

  if [[ "$final_status" != "succeeded" ]]; then
    return 1
  fi
  return 0
}

main() {
  need_cmd curl
  need_cmd jq
  need_cmd docker
  need_cmd python3

  log "Output directory: $OUT_DIR"
  log "Logging in as $DF_EMAIL"
  login
  log "Authenticated user id: $AUTH_USER_ID"

  log "Resolving user id from database"
  pick_user_id_from_db
  log "DB user id: $DB_USER_ID"

  if [[ "$AUTH_USER_ID" != "$DB_USER_ID" ]]; then
    log "WARNING: auth user id and DB user id differ; continuing with DB artifact lookup for ${DF_EMAIL}"
  fi

  if [[ -z "$FACE_ARTIFACT_ID" ]]; then
    log "Picking face artifact id from database"
    pick_face_artifact_id "$DB_USER_ID"
  fi
  if [[ -z "$AUDIO_ARTIFACT_ID" ]]; then
    log "Picking audio artifact id from database"
    pick_audio_artifact_id "$DB_USER_ID"
  fi

  log "FACE_ARTIFACT_ID=$FACE_ARTIFACT_ID"
  log "AUDIO_ARTIFACT_ID=$AUDIO_ARTIFACT_ID"

  show_artifact_debug "$FACE_ARTIFACT_ID" "face"
  show_artifact_debug "$AUDIO_ARTIFACT_ID" "audio"

  build_payload
  run_preview

  log "Creating Hedra fusion job"
  create_job
  log "JOB_ID=$JOB_ID"

  log "Polling job status"
  poll_job

  log "Printing summary"
  print_summary
}

main "$@"
