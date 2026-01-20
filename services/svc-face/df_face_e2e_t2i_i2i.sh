set -euo pipefail

# ==============================================================================
# DesiFaces svc-face E2E Test (T2I + I2I)
#
# Login:  email=user1@desifaces.ai  password=password1
# Runs:
#   1) T2I job: generates N variants from a text prompt
#   2) I2I job: uploads a source image -> generates N variants with preservation_strength
#
# Requirements: curl, jq
# ==============================================================================
command -v curl >/dev/null || { echo "Missing: curl"; exit 1; }
command -v jq   >/dev/null || { echo "Missing: jq"; exit 1; }

# --- Config (override via env) ---
CORE_BASE="${CORE_BASE:-http://localhost:8000}"      # svc-core auth endpoint
FACE_BASE="${FACE_BASE:-http://localhost:8003}"      # svc-face endpoint
EMAIL="${EMAIL:-user1@desifaces.ai}"
PASSWORD="${PASSWORD:-password1}"

# I2I source image (override)
IMG_LOCAL="${IMG_LOCAL:-/home/azureuser/workspace/desifaces-v2/download.png}"

# Test params
NUM_VARIANTS="${NUM_VARIANTS:-2}"
POLL_SECS="${POLL_SECS:-2}"
TIMEOUT_SECS="${TIMEOUT_SECS:-240}"

# T2I prompt
T2I_PROMPT="${T2I_PROMPT:-confident Tamilnadu traditional, vibrant colors, intricate patterns, elaborate jewelry, thick jasmine flower mala, ornate hair accessories, ultra realistic studio portrait, natural lighting, sharp focus, professional resolution, professional photography,}"

# I2I prompt (try a strong wardrobe+bg request)
I2I_PROMPT="${I2I_PROMPT:-Same person, same identity. Change outfit to a professional Tamilnadu-inspired maroon saree with subtle bright gold border, thick jasmine flower mala . Change background to a traditional Tamilnadu wedding setting. Keep face, age, hair, skin tone unchanged.}"

# Product semantics: 1.0 => preserve identity more (less change), 0.0 => more change
PRESERVATION_STRENGTH="${PRESERVATION_STRENGTH:-0.85}"

# Output dir
OUT_DIR="${OUT_DIR:-/tmp/df_e2e_$(date +%s)}"
mkdir -p "$OUT_DIR"

log()  { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------------------
log "Logging in via CORE_BASE=$CORE_BASE as $EMAIL ..."
LOGIN_JSON="$(mktemp)"
curl -sS -o "$LOGIN_JSON" -w "\nHTTP=%{http_code}\n" \
  -X POST "$CORE_BASE/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" \
  | tail -n1 | grep -q "HTTP=200" || {
    echo "Login response:"
    cat "$LOGIN_JSON" | jq || cat "$LOGIN_JSON"
    die "Login failed"
  }

DF_TOKEN="$(cat "$LOGIN_JSON" | jq -r '.access_token // .token // empty')"
[ -n "$DF_TOKEN" ] || { cat "$LOGIN_JSON" | jq || true; die "Could not extract access_token"; }
AUTH=(-H "Authorization: Bearer $DF_TOKEN")

log "Login OK. Token acquired."
log "OUT_DIR=$OUT_DIR"

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
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
      echo "Upload response:"
      cat "$out_json" | jq || cat "$out_json"
      die "Upload failed"
    }

  local url
  url="$(cat "$out_json" | jq -r '.image_url // .source_image_url // .url // .data.url // empty')"
  [ -n "$url" ] || { cat "$out_json" | jq || true; die "Upload succeeded but no image_url returned"; }
  echo "$url"
}

create_job() {
  local payload_json="$1"
  local out_json="$OUT_DIR/create_job.json"
  curl -sS -o "$out_json" -w "\nHTTP=%{http_code}\n" \
    -X POST "$FACE_BASE/api/face/creator/generate" \
    "${AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "$payload_json" \
    | tail -n1 | grep -q "HTTP=200" || {
      echo "Create job response:"
      cat "$out_json" | jq || cat "$out_json"
      die "Create job failed"
    }

  local job_id
  job_id="$(cat "$out_json" | jq -r '.job_id // empty')"
  [ -n "$job_id" ] || { cat "$out_json" | jq || true; die "No job_id in response"; }
  echo "$job_id"
}

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >&2; }   # <-- stderr
die() { echo "ERROR: $*" >&2; exit 1; }

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

    # IMPORTANT: don't print anything from curl to stdout
    curl -sS -o "$status_json" -w "HTTP=%{http_code}\n" \
      -X GET "$FACE_BASE/api/face/creator/jobs/${job_id}/status" \
      "${AUTH[@]}" \
      | tail -n1 | grep -q "HTTP=200" || {
        log "Status poll failed; response:"
        cat "$status_json" | jq >&2 || cat "$status_json" >&2
        die "Status poll failed"
      }

    st="$(jq -r '.status // empty' "$status_json")"
    log "Job $job_id status=$st (elapsed ${elapsed}s)"

    if [ "$st" = "succeeded" ]; then
      echo "$status_json"          # <-- ONLY stdout output from this function
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
  n="$(cat "$status_json" | jq -r '.variants | length')"
  [ "$n" -gt 0 ] || die "No variants in status response"

  log "Downloading $n variants..."
  for i in $(seq 0 $((n - 1))); do
    local url
    url="$(cat "$status_json" | jq -r ".variants[$i].image_url // empty")"
    [ -n "$url" ] || { echo "Missing image_url for variant index $i"; continue; }
    local out="$OUT_DIR/${prefix}_variant_$((i+1)).jpg"
    curl -sL "$url" -o "$out"
    log "Saved: $out ($(ls -lh "$out" | awk '{print $5}'))"
  done
}

# ------------------------------------------------------------------------------
# 1) T2I E2E
# ------------------------------------------------------------------------------
log "=== T2I E2E ==="
T2I_PAYLOAD="$(jq -cn \
  --arg language "en" \
  --arg user_prompt "$T2I_PROMPT" \
  --argjson num_variants "$NUM_VARIANTS" \
  '{
    language: $language,
    user_prompt: $user_prompt,
    num_variants: $num_variants,
    mode: "text-to-image",

    # Optional structured inputs (safe defaults; customize if you want)
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

T2I_JOB_ID="$(create_job "$T2I_PAYLOAD")"
log "T2I job_id=$T2I_JOB_ID"
T2I_STATUS_JSON="$(poll_job "$T2I_JOB_ID")"
download_variants "$T2I_STATUS_JSON" "t2i_${T2I_JOB_ID}"

# ------------------------------------------------------------------------------
# 2) I2I E2E
# ------------------------------------------------------------------------------
log "=== I2I E2E ==="
SRC_URL="$(upload_image "$IMG_LOCAL")"
log "SRC_URL=$SRC_URL"

I2I_PAYLOAD="$(jq -cn \
  --arg language "en" \
  --arg user_prompt "$I2I_PROMPT" \
  --argjson num_variants "$NUM_VARIANTS" \
  --arg source_image_url "$SRC_URL" \
  --argjson preservation_strength "$PRESERVATION_STRENGTH" \
  '{
    language: $language,
    user_prompt: $user_prompt,
    num_variants: $num_variants,
    mode: "image-to-image",
    source_image_url: $source_image_url,
    preservation_strength: $preservation_strength,

    # Optional structured inputs (these should drive wardrobe/background in prompt engine)
    region_code: "kerala",
    gender: "female",
    use_case_code: "brand_ambassador",
    style_code: "professional",
    context_code: "modern_office"
  }'
)"

I2I_JOB_ID="$(create_job "$I2I_PAYLOAD")"
log "I2I job_id=$I2I_JOB_ID"
I2I_STATUS_JSON="$(poll_job "$I2I_JOB_ID")"
download_variants "$I2I_STATUS_JSON" "i2i_${I2I_JOB_ID}"

log "✅ DONE. Outputs saved in: $OUT_DIR"