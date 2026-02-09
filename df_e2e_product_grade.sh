set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Auto-load infra/.env if present (does not overwrite already-exported vars)
if [[ -f "$ROOT/infra/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ROOT/infra/.env"
  set +a
fi

CORE_BASE="${CORE_BASE:-http://localhost:8000}"
FUSION_EXT_BASE="${FUSION_EXT_BASE:-http://localhost:8006}"
AUDIO_BASE="${AUDIO_BASE:-http://localhost:8004}"
FUSION_BASE="${FUSION_BASE:-http://localhost:8002}"

ASPECT_RATIO="${ASPECT_RATIO:-9:16}"
SEGMENT_SECONDS="${SEGMENT_SECONDS:-12}"
MAX_SEGMENT_SECONDS="${MAX_SEGMENT_SECONDS:-30}"

# Optional voice gender controls
VOICE_GENDER_MODE="${VOICE_GENDER_MODE:-auto}"   # auto|manual
VOICE_GENDER="${VOICE_GENDER:-}"                # male|female|empty

TIMEOUT_S="${TIMEOUT_S:-2400}"   # 5-min script will take longer; bump default
POLL_S="${POLL_S:-3}"
USE_DOCKER_LOGS="${USE_DOCKER_LOGS:-1}"

: "${EMAIL:?Set EMAIL (e.g., user1@desifaces.ai)}"
: "${PASSWORD:?Set PASSWORD}"
: "${SVC_TO_SVC_BEARER:?Set SVC_TO_SVC_BEARER (shared svc secret)}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

ACCESS_TOKEN=""
REFRESH_TOKEN=""
USER_ID=""
JOB_ID=""
FACE_ARTIFACT_ID="${FACE_ARTIFACT_ID:-}"

# ---- logging helpers (stderr so they don't pollute captured stdout) ----
ok()   { echo -e "✅ $*" >&2; }
info() { echo -e "ℹ️  $*" >&2; }
warn() { echo -e "⚠️  $*" >&2; }

fail() {
  echo -e "❌ $*" >&2

  echo -e "\n---- LAST RESPONSE BODY ----" >&2
  if [[ -f "$TMPDIR/out.json" ]]; then
    jq . <"$TMPDIR/out.json" 2>/dev/null || cat "$TMPDIR/out.json" >&2
  fi

  if [[ -n "${JOB_ID:-}" ]]; then
    echo -e "\n---- DB: longform_jobs ----" >&2
    docker compose --env-file "$ROOT/infra/.env" exec -T desifaces-db \
      psql -U desifaces_admin -d desifaces -v ON_ERROR_STOP=1 -c "
select id,status,error_code,error_message,created_at,updated_at,completed_segments,total_segments,final_storage_path
from public.longform_jobs
where id='${JOB_ID}'::uuid;" || true

    echo -e "\n---- DB: longform_segments (summary) ----" >&2
    docker compose --env-file "$ROOT/infra/.env" exec -T desifaces-db \
      psql -U desifaces_admin -d desifaces -v ON_ERROR_STOP=1 -c "
select segment_index,status,error_code,left(coalesce(error_message,''),120) as err
from public.longform_segments
where job_id='${JOB_ID}'::uuid
order by segment_index asc;" || true
  fi

  if [[ "$USE_DOCKER_LOGS" == "1" ]] && command -v docker >/dev/null 2>&1; then
    warn "---- docker logs (tail) ----"
    docker logs -n 200 df-svc-fusion-extension-worker 2>/dev/null || true
    docker logs -n 200 df-svc-fusion-extension 2>/dev/null || true
    docker logs -n 200 df-svc-fusion 2>/dev/null || true
    docker logs -n 200 df-svc-audio 2>/dev/null || true
  fi

  exit 1
}

need() { command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"; }
need curl
need jq
need docker

# ---- SCRIPT_TEXT loading (supports long text cleanly) ----
SCRIPT_TEXT_FILE="${SCRIPT_TEXT_FILE:-}"
SCRIPT_TEXT="${SCRIPT_TEXT:-}"

if [[ -n "$SCRIPT_TEXT_FILE" ]]; then
  [[ -f "$SCRIPT_TEXT_FILE" ]] || fail "SCRIPT_TEXT_FILE not found: $SCRIPT_TEXT_FILE"
  SCRIPT_TEXT="$(cat "$SCRIPT_TEXT_FILE")"
fi

if [[ -z "${SCRIPT_TEXT// }" ]]; then
  # default minimal text if caller didn't supply
  SCRIPT_TEXT="Hello from DesiFaces longform test. This is a short E2E validation run. Goodbye."
fi

# ---- normalize voice gender env vars to prevent 422 mistakes ----
VOICE_GENDER_MODE="$(echo "$VOICE_GENDER_MODE" | tr '[:upper:]' '[:lower:]' | xargs || true)"
VOICE_GENDER="$(echo "$VOICE_GENDER" | tr '[:upper:]' '[:lower:]' | xargs || true)"

if [[ "$VOICE_GENDER_MODE" != "auto" && "$VOICE_GENDER_MODE" != "manual" ]]; then
  warn "VOICE_GENDER_MODE invalid ($VOICE_GENDER_MODE); forcing 'auto'"
  VOICE_GENDER_MODE="auto"
fi
if [[ -n "$VOICE_GENDER" && "$VOICE_GENDER" != "male" && "$VOICE_GENDER" != "female" ]]; then
  warn "VOICE_GENDER invalid ($VOICE_GENDER); clearing"
  VOICE_GENDER=""
fi

# ---- http helpers ----
http_status() {
  local method="$1"; shift
  local url="$1"; shift
  local out_file="$TMPDIR/out.json"
  : >"$out_file"

  if [[ "$method" == "GET" ]]; then
    curl -sS -o "$out_file" -w "%{http_code}" "$url" "$@"
    return
  fi

  local data_file="$1"; shift
  curl -sS -o "$out_file" -w "%{http_code}" -X "$method" \
    -H "Content-Type: application/json" --data-binary @"$data_file" "$url" "$@"
}

body() { cat "$TMPDIR/out.json"; }

login() {
  info "Login (JWT user flow) ..."
  cat >"$TMPDIR/login.json" <<JSON
{"email":"$EMAIL","password":"$PASSWORD"}
JSON
  local code
  code="$(http_status POST "$CORE_BASE/api/auth/login" "$TMPDIR/login.json")" || true
  [[ "$code" == "200" ]] || fail "Login failed (HTTP $code)"

  ACCESS_TOKEN="$(body | jq -r '.access_token // empty')"
  REFRESH_TOKEN="$(body | jq -r '.refresh_token // empty')"

  [[ -n "$ACCESS_TOKEN" ]] || fail "login: access_token missing"
  [[ -n "$REFRESH_TOKEN" ]] || fail "login: refresh_token missing"

  ok "Login OK (expires_in=$(body | jq -r '.expires_in // "?"'))"
}

refresh_access() {
  cat >"$TMPDIR/refresh.json" <<JSON
{"refresh_token":"$REFRESH_TOKEN"}
JSON
  local code
  code="$(http_status POST "$CORE_BASE/api/auth/refresh" "$TMPDIR/refresh.json")" || true
  [[ "$code" == "200" ]] || fail "Refresh failed (HTTP $code)"
  ACCESS_TOKEN="$(body | jq -r '.access_token // empty')"
  [[ -n "$ACCESS_TOKEN" ]] || fail "refresh: access_token missing"
}

call_user() {
  local method="$1"; shift
  local url="$1"; shift
  local json_payload="$1"; shift

  echo "$json_payload" >"$TMPDIR/req.json"

  local code
  code="$(http_status "$method" "$url" "$TMPDIR/req.json" -H "Authorization: Bearer $ACCESS_TOKEN")" || true

  if [[ "$code" == "401" ]] && grep -qiE "expired" "$TMPDIR/out.json"; then
    warn "User JWT expired; refreshing and retrying once..."
    refresh_access
    code="$(http_status "$method" "$url" "$TMPDIR/req.json" -H "Authorization: Bearer $ACCESS_TOKEN")" || true
  fi

  echo "$code"
}

call_user_get() {
  local url="$1"
  local code
  code="$(http_status GET "$url" -H "Authorization: Bearer $ACCESS_TOKEN")" || true

  if [[ "$code" == "401" ]] && grep -qiE "expired" "$TMPDIR/out.json"; then
    warn "User JWT expired; refreshing and retrying once..."
    refresh_access
    code="$(http_status GET "$url" -H "Authorization: Bearer $ACCESS_TOKEN")" || true
  fi

  echo "$code"
}

health_checks() {
  info "Health checks ..."
  curl -sS "$CORE_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-core health failed"
  curl -sS "$AUDIO_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-audio health failed"
  curl -sS "$FUSION_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-fusion health failed"
  curl -sS "$FUSION_EXT_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-fusion-extension health failed"
  ok "All health endpoints OK"
}

resolve_user_id_from_db() {
  info "Resolve USER_ID from DB (core.users.email) ..."
  USER_ID="$(
    docker compose --env-file "$ROOT/infra/.env" exec -T desifaces-db \
      psql -U desifaces_admin -d desifaces -Atc \
      "select id::text from core.users where lower(email)=lower('$EMAIL') limit 1;"
  )"
  [[ -n "$USER_ID" ]] || fail "Could not resolve USER_ID from DB for email=$EMAIL"
  ok "USER_ID=$USER_ID"
}

resolve_face_artifact_id_if_missing() {
  if [[ -n "$FACE_ARTIFACT_ID" ]]; then
    ok "Using provided FACE_ARTIFACT_ID=$FACE_ARTIFACT_ID"
  else
    info "Resolve latest FACE_ARTIFACT_ID from DB (face_job_outputs -> media_assets) ..."
    FACE_ARTIFACT_ID="$(
      docker compose --env-file "$ROOT/infra/.env" exec -T desifaces-db \
        psql -U desifaces_admin -d desifaces -Atc "
select m.id::text
from public.face_job_outputs o
join public.media_assets m on m.id = o.output_asset_id
where m.user_id = '$USER_ID'::uuid
order by m.created_at desc
limit 1;"
    )"
    [[ -n "$FACE_ARTIFACT_ID" ]] || fail "Could not auto-resolve FACE_ARTIFACT_ID (generate a face first)"
    ok "Auto FACE_ARTIFACT_ID=$FACE_ARTIFACT_ID"
  fi

  local owner
  owner="$(
    docker compose --env-file "$ROOT/infra/.env" exec -T desifaces-db \
      psql -U desifaces_admin -d desifaces -Atc \
      "select user_id::text from public.media_assets where id='$FACE_ARTIFACT_ID'::uuid;"
  )"
  [[ -n "$owner" ]] || fail "FACE_ARTIFACT_ID not found in public.media_assets: $FACE_ARTIFACT_ID"
  [[ "$owner" == "$USER_ID" ]] || fail "FACE_ARTIFACT_ID belongs to different user: owner=$owner expected=$USER_ID"
}

negative_auth_tests() {
  info "Negative auth tests (prove enforcement) ..."

  echo '{}' >"$TMPDIR/empty.json"

  local code
  code="$(http_status POST "$AUDIO_BASE/api/audio/tts" "$TMPDIR/empty.json")" || true
  [[ "$code" == "401" ]] || fail "Expected svc-audio /api/audio/tts without token -> 401 (got $code)"
  ok "svc-audio rejects missing token (401)"

  code="$(http_status POST "$FUSION_BASE/jobs" "$TMPDIR/empty.json")" || true
  [[ "$code" == "401" ]] || fail "Expected svc-fusion /jobs without token -> 401 (got $code)"
  ok "svc-fusion rejects missing token (401)"

  code="$(http_status POST "$AUDIO_BASE/api/audio/tts" "$TMPDIR/empty.json" -H "Authorization: Bearer $SVC_TO_SVC_BEARER")" || true
  [[ "$code" == "401" ]] || fail "Expected svc-audio svc-token without actor -> 401 (got $code)"
  ok "svc-audio enforces actor header for svc token (HTTP 401)"
}

svc_to_svc_smoke() {
  info "Service-to-service auth tests (svc bearer + actor header) ..."

  cat >"$TMPDIR/tts.json" <<JSON
{"text":"svc-to-svc hello","target_locale":"en-US"}
JSON

  local code
  code="$(http_status POST "$AUDIO_BASE/api/audio/tts" "$TMPDIR/tts.json" \
    -H "Authorization: Bearer $SVC_TO_SVC_BEARER" \
    -H "X-Actor-User-Id: $USER_ID")" || true
  [[ "$code" == "200" || "$code" == "202" ]] || fail "svc-audio svc-to-svc should be 200/202 (got $code)"
  ok "svc-audio svc-to-svc OK (HTTP $code)"

  echo '{}' >"$TMPDIR/empty.json"
  code="$(http_status POST "$FUSION_BASE/jobs" "$TMPDIR/empty.json" \
    -H "Authorization: Bearer $SVC_TO_SVC_BEARER" \
    -H "X-Actor-User-Id: $USER_ID")" || true
  [[ "$code" == "422" ]] || fail "svc-fusion svc-to-svc should be 422 on empty body (auth accepted) (got $code)"
  ok "svc-fusion svc-to-svc auth accepted (422 validation as expected)"
}

create_longform_job_as_user() {
  info "Create longform job as USER (JWT) ..."
  local payload

  payload="$(jq -nc \
    --arg face_artifact_id "$FACE_ARTIFACT_ID" \
    --arg aspect_ratio "$ASPECT_RATIO" \
    --arg script_text "$SCRIPT_TEXT" \
    --arg voice_gender_mode "$VOICE_GENDER_MODE" \
    --arg voice_gender "$VOICE_GENDER" \
    --argjson segment_seconds "$SEGMENT_SECONDS" \
    --argjson max_segment_seconds "$MAX_SEGMENT_SECONDS" \
    '
    {
      face_artifact_id: $face_artifact_id,
      aspect_ratio: $aspect_ratio,
      voice_cfg: { locale: "en-US" },
      segment_seconds: $segment_seconds,
      max_segment_seconds: $max_segment_seconds,
      script_text: $script_text,
      tags: { source: "df_e2e_product_grade" }
    }
    + (if ($voice_gender_mode|length) > 0 then { voice_gender_mode: $voice_gender_mode } else {} end)
    + (if ($voice_gender|length) > 0 then { voice_gender: $voice_gender } else {} end)
    '
  )"

  local code
  code="$(call_user POST "$FUSION_EXT_BASE/api/longform/jobs" "$payload")" || true
  [[ "$code" == "200" || "$code" == "201" ]] || fail "Create longform job failed (HTTP $code)"

  JOB_ID="$(body | jq -r '.job_id // .id // empty')"
  [[ -n "$JOB_ID" ]] || fail "Create longform job: missing job_id/id in response"
  ok "Created longform job: $JOB_ID"
}

poll_longform() {
  info "Poll longform job until done (timeout=${TIMEOUT_S}s) ..."
  local deadline=$(( $(date +%s) + TIMEOUT_S ))

  while true; do
    local code
    code="$(call_user_get "$FUSION_EXT_BASE/api/longform/jobs/$JOB_ID")" || true
    [[ "$code" == "200" ]] || fail "Get longform job failed (HTTP $code)"

    local status done total
    status="$(body | jq -r '.status // ""' | tr '[:upper:]' '[:lower:]')"
    done="$(body | jq -r '.completed_segments // 0')"
    total="$(body | jq -r '.total_segments // 0')"
    info "  status=$status (completed=$done/$total)"

    if [[ "$status" == "succeeded" ]]; then
      ok "Longform job succeeded"
      body | jq . >&2
      return
    fi
    if [[ "$status" == "failed" ]]; then
      body | jq . >&2
      fail "Longform job failed"
    fi

    if (( $(date +%s) > deadline )); then
      body | jq . >&2
      fail "Timed out waiting for longform completion"
    fi
    sleep "$POLL_S"
  done
}

verify_segments() {
  info "Verify segments have video URLs ..."
  local code
  code="$(call_user_get "$FUSION_EXT_BASE/api/longform/jobs/$JOB_ID/segments")" || true
  [[ "$code" == "200" ]] || fail "List segments failed (HTTP $code)"

  local count missing
  count="$(body | jq 'length')"
  [[ "$count" -ge 1 ]] || fail "Expected >= 1 segment, got $count"

  missing="$(body | jq '[.[] | select((.segment_video_url // "") == "")] | length')"
  [[ "$missing" == "0" ]] || fail "Some segments missing segment_video_url ($missing segments)"

  ok "All segments have segment_video_url"
  body | jq . >&2
}

# ----------------- MAIN -----------------
health_checks
login
resolve_user_id_from_db
resolve_face_artifact_id_if_missing
negative_auth_tests
svc_to_svc_smoke
create_longform_job_as_user
poll_longform
verify_segments

ok "PRODUCT-GRADE E2E PASS ✅"