set -euo pipefail

# =========================
# Product-grade E2E: Auth + Longform + Svc-to-Svc
# =========================
# Required env:
#   EMAIL, PASSWORD, FACE_ARTIFACT_ID, SVC_TO_SVC_BEARER
#
# Optional env overrides:
#   CORE_BASE (default http://localhost:8000)
#   FUSION_EXT_BASE (default http://localhost:8006)
#   AUDIO_BASE (default http://localhost:8004)
#   FUSION_BASE (default http://localhost:8002)
#   ASPECT_RATIO (default 9:16)
#   SEGMENT_SECONDS (default 12)
#   MAX_SEGMENT_SECONDS (default 30)
#   SCRIPT_TEXT (default small 3-sentence)
#   TIMEOUT_S (default 1200)
#   POLL_S (default 5)
#   USE_DOCKER_LOGS (default 1) - prints container logs on failure if docker present
#
# Notes:
# - This script tests both JWT (user flow) and svc-to-svc (internal worker flow).
# - It purposely runs negative tests first to prove auth enforcement.

CORE_BASE="${CORE_BASE:-http://localhost:8000}"
FUSION_EXT_BASE="${FUSION_EXT_BASE:-http://localhost:8006}"
AUDIO_BASE="${AUDIO_BASE:-http://localhost:8004}"
FUSION_BASE="${FUSION_BASE:-http://localhost:8002}"

ASPECT_RATIO="${ASPECT_RATIO:-9:16}"
SEGMENT_SECONDS="${SEGMENT_SECONDS:-12}"
MAX_SEGMENT_SECONDS="${MAX_SEGMENT_SECONDS:-30}"
SCRIPT_TEXT="${SCRIPT_TEXT:-Hello from segment one. Hello from segment two. Hello from segment three.}"

TIMEOUT_S="${TIMEOUT_S:-1200}"
POLL_S="${POLL_S:-5}"
USE_DOCKER_LOGS="${USE_DOCKER_LOGS:-1}"

: "${EMAIL:?Set EMAIL (e.g., user1@desifaces.ai)}"
: "${PASSWORD:?Set PASSWORD}"
: "${FACE_ARTIFACT_ID:?Set FACE_ARTIFACT_ID (UUID)}"
: "${SVC_TO_SVC_BEARER:?Set SVC_TO_SVC_BEARER (the shared svc secret)}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

ACCESS_TOKEN=""
REFRESH_TOKEN=""
USER_ID=""

# -------- pretty printing ----------
ok()   { echo -e "✅ $*"; }
info() { echo -e "ℹ️  $*"; }
warn() { echo -e "⚠️  $*"; }
fail() {
  echo -e "❌ $*"
  if [[ "$USE_DOCKER_LOGS" == "1" ]] && command -v docker >/dev/null 2>&1; then
    warn "---- docker logs (tail) ----"
    docker logs -n 120 df-svc-fusion-extension-worker 2>/dev/null || true
    docker logs -n 120 df-svc-fusion 2>/dev/null || true
    docker logs -n 120 df-svc-audio 2>/dev/null || true
  fi
  exit 1
}

need_jq() { command -v jq >/dev/null 2>&1 || fail "jq is required"; }
need_jq

# -------- HTTP helpers ----------
http_status() {
  # usage: http_status METHOD URL JSON_FILE HEADERS...
  local method="$1"; shift
  local url="$1"; shift
  local data_file="$1"; shift
  local out_file="$TMPDIR/out.json"
  local hdr_args=()
  while [[ $# -gt 0 ]]; do hdr_args+=(-H "$1"); shift; done

  if [[ "$method" == "GET" ]]; then
    curl -sS -o "$out_file" -w "%{http_code}" "${hdr_args[@]}" "$url"
  else
    curl -sS -o "$out_file" -w "%{http_code}" -X "$method" "${hdr_args[@]}" \
      -H "Content-Type: application/json" --data-binary @"$data_file" "$url"
  fi
}

http_body() { cat "$TMPDIR/out.json"; }

require_status() {
  local got="$1"; local want="$2"; local ctx="$3"
  [[ "$got" == "$want" ]] || {
    echo "---- response body ----"
    http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "$ctx (expected HTTP $want, got $got)"
  }
}

# -------- Auth flows ----------
login() {
  info "Login (JWT user flow) ..."
  cat > "$TMPDIR/login.json" <<JSON
{"email":"$EMAIL","password":"$PASSWORD"}
JSON

  local code
  code="$(http_status POST "$CORE_BASE/api/auth/login" "$TMPDIR/login.json")" || true
  [[ "$code" == "200" ]] || {
    echo "---- login body ----"
    http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "Login failed"
  }

  ACCESS_TOKEN="$(http_body | jq -r '.access_token // empty')"
  REFRESH_TOKEN="$(http_body | jq -r '.refresh_token // empty')"

  [[ -n "$ACCESS_TOKEN" ]] || fail "login: access_token missing"
  [[ -n "$REFRESH_TOKEN" ]] || fail "login: refresh_token missing"

  USER_ID="$(python3 - <<PY
    import base64, json, sys
    t = """$ACCESS_TOKEN""".strip()
    parts = t.split(".")
    if len(parts) < 2:
        print("")
        sys.exit(0)
    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    print(data.get("sub",""))
    PY
  )"
  [[ -n "$USER_ID" ]] || fail "login: could not parse sub from access token"

  ok "Login OK (user_id=$USER_ID, expires_in=$(http_body | jq -r '.expires_in // "?"'))"
}

refresh_access() {
  info "Refresh access token ..."
  cat > "$TMPDIR/refresh.json" <<JSON
{"refresh_token":"$REFRESH_TOKEN"}
JSON
  local code
  code="$(http_status POST "$CORE_BASE/api/auth/refresh" "$TMPDIR/refresh.json")" || true
  [[ "$code" == "200" ]] || {
    echo "---- refresh body ----"
    http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "Refresh failed"
  }
  ACCESS_TOKEN="$(http_body | jq -r '.access_token // empty')"
  [[ -n "$ACCESS_TOKEN" ]] || fail "refresh: access_token missing"
  ok "Refresh OK"
}

auth_header_user() { echo "Authorization: Bearer $ACCESS_TOKEN"; }
auth_header_svc()  { echo "Authorization: Bearer $SVC_TO_SVC_BEARER"; }

# Auto-refresh on "Signature has expired" from any call that uses USER token.
call_user_json() {
  # usage: call_user_json METHOD URL JSON_STRING
  local method="$1"; shift
  local url="$1"; shift
  local json="$1"; shift

  echo "$json" > "$TMPDIR/req.json"
  local code
  code="$(http_status "$method" "$url" "$TMPDIR/req.json" "$(auth_header_user)")" || true

  if [[ "$code" == "401" ]]; then
    if http_body | rg -q "Signature has expired|expired"; then
      warn "User JWT expired; refreshing and retrying once..."
      refresh_access
      code="$(http_status "$method" "$url" "$TMPDIR/req.json" "$(auth_header_user)")" || true
    fi
  fi

  echo "$code"
}

call_user_get() {
  local url="$1"
  local code
  code="$(http_status GET "$url" "$TMPDIR/req.json" "$(auth_header_user)")" || true
  if [[ "$code" == "401" ]]; then
    if http_body | rg -q "Signature has expired|expired"; then
      warn "User JWT expired; refreshing and retrying once..."
      refresh_access
      code="$(http_status GET "$url" "$TMPDIR/req.json" "$(auth_header_user)")" || true
    fi
  fi
  echo "$code"
}

# -------- Test sections ----------
health_checks() {
  info "Health checks ..."
  curl -sS "$CORE_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-core health failed"
  curl -sS "$AUDIO_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-audio health failed"
  curl -sS "$FUSION_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-fusion health failed"
  curl -sS "$FUSION_EXT_BASE/api/health" | jq -e '.status=="ok"' >/dev/null || fail "svc-fusion-extension health failed"
  ok "All health endpoints OK"
}

negative_auth_tests() {
  info "Negative auth tests (prove enforcement) ..."

  # svc-audio should reject missing token
  echo '{}' > "$TMPDIR/req.json"
  local code
  code="$(http_status POST "$AUDIO_BASE/api/audio/tts" "$TMPDIR/req.json")" || true
  [[ "$code" == "401" ]] || fail "Expected svc-audio POST /api/audio/tts without token to be 401, got $code"
  ok "svc-audio rejects missing token (401)"

  # svc-fusion should reject missing token
  code="$(http_status POST "$FUSION_BASE/jobs" "$TMPDIR/req.json")" || true
  [[ "$code" == "401" ]] || fail "Expected svc-fusion POST /jobs without token to be 401, got $code"
  ok "svc-fusion rejects missing token (401)"

  # svc-audio should reject service token WITHOUT actor header
  code="$(http_status POST "$AUDIO_BASE/api/audio/tts" "$TMPDIR/req.json" "$(auth_header_svc)")" || true
  [[ "$code" == "401" ]] || {
    echo "---- body ----"; http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "Expected svc-audio service call without actor header to be 401"
  }
  ok "svc-audio requires X-Actor-User-Id for service token (401)"
}

svc_to_svc_auth_tests() {
  info "Service-to-service auth tests (svc bearer + actor header) ..."

  # svc-audio minimal valid payload should get 200/202/queued etc
  cat > "$TMPDIR/tts.json" <<JSON
{"text":"hello","target_locale":"en-US"}
JSON
  local code
  code="$(http_status POST "$AUDIO_BASE/api/audio/tts" "$TMPDIR/tts.json" \
    "$(auth_header_svc)" "X-Actor-User-Id: $USER_ID")" || true

  if [[ "$code" != "200" && "$code" != "202" ]]; then
    echo "---- body ----"; http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "svc-audio svc-to-svc call failed (expected 200/202, got $code)"
  fi
  ok "svc-audio svc-to-svc OK (HTTP $code)"

  # svc-fusion should accept service token but needs a proper body; expect 422 (validation) not 401
  echo '{}' > "$TMPDIR/req.json"
  code="$(http_status POST "$FUSION_BASE/jobs" "$TMPDIR/req.json" \
    "$(auth_header_svc)" "X-Actor-User-Id: $USER_ID")" || true

  [[ "$code" == "422" ]] || {
    echo "---- body ----"; http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "Expected svc-fusion svc-to-svc POST /jobs with empty body to be 422, got $code"
  }
  ok "svc-fusion svc-to-svc auth accepted (422 validation as expected)"
}

create_longform_job_as_user() {
  info "Create longform job as USER (JWT) ..."
  local payload
  payload="$(jq -nc \
    --arg face_artifact_id "$FACE_ARTIFACT_ID" \
    --arg aspect_ratio "$ASPECT_RATIO" \
    --arg script_text "$SCRIPT_TEXT" \
    --argjson segment_seconds "$SEGMENT_SECONDS" \
    --argjson max_segment_seconds "$MAX_SEGMENT_SECONDS" \
    '{
      face_artifact_id: $face_artifact_id,
      aspect_ratio: $aspect_ratio,
      voice_cfg: { target_locale: "en-US" },
      segment_seconds: $segment_seconds,
      max_segment_seconds: $max_segment_seconds,
      script_text: $script_text,
      tags: { source: "product-grade-e2e" }
    }')"

  local code
  code="$(call_user_json POST "$FUSION_EXT_BASE/api/longform/jobs" "$payload")" || true
  [[ "$code" == "200" || "$code" == "201" ]] || {
    echo "---- body ----"; http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "Create longform job failed (HTTP $code)"
  }

  local job_id
  job_id="$(http_body | jq -r '.job_id // .id // empty')"
  [[ -n "$job_id" ]] || fail "Create longform job: missing job_id/id in response"

  ok "Created longform job: $job_id"
  echo "$job_id"
}

poll_longform_until_done() {
  local job_id="$1"
  info "Poll longform job until done (timeout=${TIMEOUT_S}s) ..."
  local deadline=$(( $(date +%s) + TIMEOUT_S ))

  while true; do
    local code
    code="$(call_user_get "$FUSION_EXT_BASE/api/longform/jobs/$job_id")" || true
    [[ "$code" == "200" ]] || {
      echo "---- body ----"; http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
      fail "Get longform job failed (HTTP $code)"
    }

    local status
    status="$(http_body | jq -r '.status // empty' | tr '[:upper:]' '[:lower:]')"

    info "  status=$status (completed=$(http_body | jq -r '.completed_segments // 0')/$(http_body | jq -r '.total_segments // 0'))"

    if [[ "$status" == "succeeded" ]]; then
      ok "Longform job succeeded"
      http_body | jq .
      break
    fi

    if [[ "$status" == "failed" ]]; then
      echo "---- job body ----"
      http_body | jq .
      fail "Longform job failed"
    fi

    if (( $(date +%s) > deadline )); then
      echo "---- job body ----"
      http_body | jq .
      fail "Timed out waiting for longform completion"
    fi

    sleep "$POLL_S"
  done
}

verify_segments() {
  local job_id="$1"
  info "Verify segments have video URLs ..."
  local code
  code="$(call_user_get "$FUSION_EXT_BASE/api/longform/jobs/$job_id/segments")" || true
  [[ "$code" == "200" ]] || {
    echo "---- body ----"; http_body | jq . 2>/dev/null || cat "$TMPDIR/out.json"
    fail "List segments failed (HTTP $code)"
  }

  local count
  count="$(http_body | jq 'length')"
  [[ "$count" -ge 1 ]] || fail "Expected >= 1 segment, got $count"

  # Ensure each segment has a segment_video_url (or equivalent)
  local missing
  missing="$(http_body | jq '[.[] | select((.segment_video_url // .video_url // "") == "")] | length')"
  [[ "$missing" == "0" ]] || {
    echo "---- segments ----"
    http_body | jq .
    fail "Some segments missing video URL ($missing segments)"
  }

  ok "All segments have video URLs"
  http_body | jq .
}

# =========================
# MAIN
# =========================
health_checks
login
negative_auth_tests
svc_to_svc_auth_tests

JOB_ID="$(create_longform_job_as_user)"
poll_longform_until_done "$JOB_ID"
verify_segments "$JOB_ID"

ok "PRODUCT-GRADE E2E PASS ✅"
BASH

chmod +x df_e2e_product_grade.sh
echo "Created ./df_e2e_product_grade.sh"