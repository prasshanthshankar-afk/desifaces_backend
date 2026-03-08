#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# DesiFaces svc-face E2E Test (T2I + I2I + Pricing)
#
# Save as:
#   services/svc-face/app/app/scripts/e2e/df_e2e_face_with_pricing.sh
#
# Login default:
#   email=user1@desifaces.ai
#   password=password1
#
# Runs:
#   1) T2I job: creates job, asserts pricing=reserved, polls, asserts pricing=committed
#   2) I2I job: uploads source image, creates job, asserts pricing=reserved, polls, asserts pricing=committed
#
# Requirements:
#   curl, jq, docker
# ==============================================================================

command -v curl   >/dev/null || { echo "Missing: curl"; exit 1; }
command -v jq     >/dev/null || { echo "Missing: jq"; exit 1; }
command -v docker >/dev/null || { echo "Missing: docker"; exit 1; }

# ------------------------------------------------------------------------------
# Config (override via env)
# ------------------------------------------------------------------------------
CORE_BASE="${CORE_BASE:-http://localhost:8000}"
FACE_BASE="${FACE_BASE:-http://localhost:8003}"
PRICING_BASE="${PRICING_BASE:-http://localhost:8009}"

EMAIL="${EMAIL:-user1@desifaces.ai}"
PASSWORD="${PASSWORD:-password1}"

# I2I source image
IMG_LOCAL="${IMG_LOCAL:-/home/azureuser/workspace/desifaces-v2/download.png}"

# Test params
NUM_VARIANTS="${NUM_VARIANTS:-2}"
POLL_SECS="${POLL_SECS:-2}"
TIMEOUT_SECS="${TIMEOUT_SECS:-240}"

# T2I prompt
T2I_PROMPT="${T2I_PROMPT:-confident Tamilnadu traditional, vibrant colors, intricate patterns, elaborate jewelry, thick jasmine flower mala, ornate hair accessories, ultra realistic studio portrait, natural lighting, sharp focus, professional resolution, professional photography}"

# I2I prompt
I2I_PROMPT="${I2I_PROMPT:-Same person, same identity. Change outfit to a professional Tamilnadu-inspired maroon saree with subtle bright gold border, thick jasmine flower mala. Change background to a traditional Tamilnadu wedding setting. Keep face, age, hair, skin tone unchanged.}"

# Identity preservation (svc-face clamps internally)
PRESERVATION_STRENGTH="${PRESERVATION_STRENGTH:-0.85}"

# Output dir
OUT_DIR="${OUT_DIR:-/tmp/df_face_pricing_e2e_$(date +%s)}"
mkdir -p "$OUT_DIR"

# DB container name
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
check_http_200() {
  local url="$1"
  local label="$2"
  local out="${OUT_DIR}/$(echo "$label" | tr ' /:' '___').txt"

  curl -sS -o "$out" -w "\nHTTP=%{http_code}\n" "$url" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "$label failed at $url"
      cat "$out" >&2 || true
      die "$label failed"
    }
}

login() {
  log "Logging in via CORE_BASE=$CORE_BASE as $EMAIL ..."
  local login_json="$OUT_DIR/login.json"

  curl -sS -o "$login_json" -w "\nHTTP=%{http_code}\n" \
    -X POST "$CORE_BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "Login response:"
      cat "$login_json" | jq >&2 || cat "$login_json" >&2
      die "Login failed"
    }

  DF_TOKEN="$(jq -r '.access_token // .token // empty' "$login_json")"
  [ -n "${DF_TOKEN:-}" ] || {
    cat "$login_json" | jq >&2 || true
    die "Could not extract access_token"
  }

  AUTH=(-H "Authorization: Bearer $DF_TOKEN")
  log "Login OK."
}

check_pricing_health() {
  log "Checking svc-pricing health at $PRICING_BASE ..."
  check_http_200 "${PRICING_BASE}/api/health" "pricing_health"
  log "svc-pricing health OK."
}

upload_image() {
  local file_path="$1"
  [ -f "$file_path" ] || die "Missing image file: $file_path"

  local out_json="$OUT_DIR/upload_$(basename "$file_path").json"
  log "Uploading image: $file_path"

  curl -sS -o "$out_json" -w "\nHTTP=%{http_code}\n" \
    -X POST "$FACE_BASE/api/face/assets/upload" \
    "${AUTH[@]}" \
    -F "file=@${file_path}" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "Upload response:"
      cat "$out_json" | jq >&2 || cat "$out_json" >&2
      die "Upload failed"
    }

  local url
  url="$(jq -r '.image_url // .source_image_url // .url // .data.url // empty' "$out_json")"
  [ -n "$url" ] || {
    cat "$out_json" | jq >&2 || true
    die "Upload succeeded but no image_url returned"
  }

  echo "$url"
}

create_job() {
  local payload_json="$1"
  local label="$2"
  local out_json="$OUT_DIR/create_job_${label}.json"

  curl -sS -o "$out_json" -w "\nHTTP=%{http_code}\n" \
    -X POST "$FACE_BASE/api/face/creator/generate" \
    "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "$payload_json" \
    | tail -n1 | grep -q "HTTP=200" || {
      log "Create job response ($label):"
      cat "$out_json" | jq >&2 || cat "$out_json" >&2
      die "Create job failed for $label"
    }

  local job_id
  job_id="$(jq -r '.job_id // empty' "$out_json")"
  [ -n "$job_id" ] || {
    cat "$out_json" | jq >&2 || true
    die "No job_id in create response for $label"
  }

  echo "$job_id"
}

poll_job() {
  local job_id="$1"
  local start_ts now_ts elapsed status_json st
  start_ts="$(date +%s)"

  while true; do
    now_ts="$(date +%s)"
    elapsed="$((now_ts - start_ts))"
    if [ "$elapsed" -gt "$TIMEOUT_SECS" ]; then
      die "Timeout waiting for job $job_id after ${TIMEOUT_SECS}s"
    fi

    status_json="$OUT_DIR/status_${job_id}.json"

    curl -sS -o "$status_json" -w "HTTP=%{http_code}\n" \
      -X GET "$FACE_BASE/api/face/creator/jobs/${job_id}/status" \
      "${AUTH[@]}" \
      | tail -n1 | grep -q "HTTP=200" || {
        log "Status poll failed; response:"
        cat "$status_json" | jq >&2 || cat "$status_json" >&2
        die "Status poll failed for $job_id"
      }

    st="$(jq -r '.status // empty' "$status_json")"
    log "Job $job_id status=$st (elapsed ${elapsed}s)"

    if [ "$st" = "succeeded" ]; then
      echo "$status_json"
      return 0
    fi

    if [ "$st" = "failed" ] || [ "$st" = "cancelled" ]; then
      log "Final status payload:"
      cat "$status_json" | jq >&2 || cat "$status_json" >&2
      die "Job $job_id ended with status=$st"
    fi

    sleep "$POLL_SECS"
  done
}

download_variants() {
  local status_json="$1"
  local prefix="$2"

  local n
  n="$(jq -r '.variants | length' "$status_json")"
  [ "${n:-0}" -gt 0 ] || die "No variants in status response for prefix=$prefix"

  log "Downloading $n variants for $prefix ..."
  for i in $(seq 0 $((n - 1))); do
    local url out
    url="$(jq -r ".variants[$i].image_url // empty" "$status_json")"
    [ -n "$url" ] || {
      log "Missing image_url for variant index $i"
      continue
    }
    out="$OUT_DIR/${prefix}_variant_$((i + 1)).jpg"
    curl -sL "$url" -o "$out"
    log "Saved: $out ($(ls -lh "$out" | awk '{print $5}'))"
  done
}

db_pricing_row_json() {
  local job_id="$1"
  docker exec -e JOB_ID="$job_id" -i "$DB_CONTAINER" bash -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At <<SQL
SELECT json_build_object(
  '\''id'\'', id::text,
  '\''status'\'', status,
  '\''payload_pricing'\'', payload_json->'\''pricing'\'',
  '\''meta_pricing'\'', meta_json->'\''pricing'\'',
  '\''error_code'\'', error_code,
  '\''error_message'\'', error_message
)
FROM public.studio_jobs
WHERE id = '\'''"$JOB_ID"''\''::uuid;
SQL
'
}

save_db_snapshot() {
  local job_id="$1"
  local label="$2"
  local out_json="$OUT_DIR/db_${label}_${job_id}.json"
  db_pricing_row_json "$job_id" > "$out_json"
  [ -s "$out_json" ] || die "DB snapshot empty for job=$job_id label=$label"
  echo "$out_json"
}

assert_pricing_state() {
  local job_id="$1"
  local expected_state="$2"
  local label="$3"

  local snap_json
  snap_json="$(save_db_snapshot "$job_id" "$label")"

  log "DB pricing snapshot [$label] for job=$job_id:"
  cat "$snap_json" | jq >&2 || cat "$snap_json" >&2

  local payload_state meta_state status
  payload_state="$(jq -r '.payload_pricing.state // empty' "$snap_json")"
  meta_state="$(jq -r '.meta_pricing.state // empty' "$snap_json")"
  status="$(jq -r '.status // empty' "$snap_json")"

  [ "$payload_state" = "$expected_state" ] || die "Expected payload_pricing.state=$expected_state for job=$job_id, got=$payload_state"
  [ "$meta_state" = "$expected_state" ] || die "Expected meta_pricing.state=$expected_state for job=$job_id, got=$meta_state"

  log "Pricing state OK for job=$job_id label=$label status=$status state=$expected_state"
}

assert_committed_fields() {
  local job_id="$1"
  local label="$2"

  local snap_json
  snap_json="$(save_db_snapshot "$job_id" "$label")"

  local state actual_units ledger_entry_id commit_status
  state="$(jq -r '.payload_pricing.state // empty' "$snap_json")"
  actual_units="$(jq -r '.payload_pricing.actual_units // empty' "$snap_json")"
  ledger_entry_id="$(jq -r '.payload_pricing.ledger_entry_id // empty' "$snap_json")"
  commit_status="$(jq -r '.payload_pricing.commit_status // empty' "$snap_json")"

  [ "$state" = "committed" ] || die "Expected committed state for job=$job_id, got=$state"
  [ -n "$actual_units" ] || die "Missing actual_units for committed job=$job_id"
  [ -n "$ledger_entry_id" ] || die "Missing ledger_entry_id for committed job=$job_id"
  [ -n "$commit_status" ] || die "Missing commit_status for committed job=$job_id"

  log "Commit fields OK for job=$job_id actual_units=$actual_units ledger_entry_id=$ledger_entry_id commit_status=$commit_status"
}

run_t2i() {
  log "=== T2I E2E + Pricing ==="

  local payload
  payload="$(jq -cn \
    --arg language "en" \
    --arg user_prompt "$T2I_PROMPT" \
    --argjson num_variants "$NUM_VARIANTS" \
    '{
      language: $language,
      user_prompt: $user_prompt,
      num_variants: $num_variants,
      mode: "text-to-image",
      age_range_code: "established_professional",
      skin_tone_code: "medium_brown",
      region_code: "kerala",
      gender: "female",
      image_format_code: "instagram_portrait",
      use_case_code: "brand_ambassador",
      style_code: "professional",
      context_code: "studio_headshot"
    }'
  )"

  local job_id
  job_id="$(create_job "$payload" "t2i")"
  log "T2I job_id=$job_id"

  assert_pricing_state "$job_id" "reserved" "after_create"

  local status_json
  status_json="$(poll_job "$job_id")"

  assert_pricing_state "$job_id" "committed" "after_success"
  assert_committed_fields "$job_id" "after_success"

  download_variants "$status_json" "t2i_${job_id}"
}

run_i2i() {
  log "=== I2I E2E + Pricing ==="

  local src_url
  src_url="$(upload_image "$IMG_LOCAL")"
  log "SRC_URL=$src_url"

  local payload
  payload="$(jq -cn \
    --arg language "en" \
    --arg user_prompt "$I2I_PROMPT" \
    --argjson num_variants "$NUM_VARIANTS" \
    --arg source_image_url "$src_url" \
    --argjson preservation_strength "$PRESERVATION_STRENGTH" \
    '{
      language: $language,
      user_prompt: $user_prompt,
      num_variants: $num_variants,
      mode: "image-to-image",
      source_image_url: $source_image_url,
      preservation_strength: $preservation_strength,
      region_code: "kerala",
      gender: "female",
      use_case_code: "brand_ambassador",
      style_code: "professional",
      context_code: "modern_office"
    }'
  )"

  local job_id
  job_id="$(create_job "$payload" "i2i")"
  log "I2I job_id=$job_id"

  assert_pricing_state "$job_id" "reserved" "after_create"

  local status_json
  status_json="$(poll_job "$job_id")"

  assert_pricing_state "$job_id" "committed" "after_success"
  assert_committed_fields "$job_id" "after_success"

  download_variants "$status_json" "i2i_${job_id}"
}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
log "OUT_DIR=$OUT_DIR"
check_pricing_health
login
run_t2i
run_i2i
log "✅ DONE. Outputs + DB pricing snapshots saved in: $OUT_DIR"