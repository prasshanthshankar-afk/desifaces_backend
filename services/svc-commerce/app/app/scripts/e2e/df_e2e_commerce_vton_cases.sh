#!/usr/bin/env bash
set -euo pipefail

CORE_URL="${CORE_URL:-http://localhost:8000}"
COMMERCE_URL="${COMMERCE_URL:-http://localhost:8008}"

EMAIL="${EMAIL:-df_service_account_new@desifaces.ai}"
PASSWORD="${PASSWORD:-desifaces_mahadev_password}"
DEVICE_ID="${DEVICE_ID:-mobile}"
CLIENT_TYPE="${CLIENT_TYPE:-ios}"

SAREE_FILE="${SAREE_FILE:-}"
POLL_SECS="${POLL_SECS:-5}"
POLL_TIMEOUT_SECS="${POLL_TIMEOUT_SECS:-900}"

# Validate output URLs via HEAD (can disable if needed)
VALIDATE_URLS="${VALIDATE_URLS:-1}"
HEAD_TIMEOUT_SECS="${HEAD_TIMEOUT_SECS:-25}"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1"; exit 2; }; }
need curl; need jq; need python3

RUN_DIR="/tmp/df_e2e_commerce_vendor_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"

log(){ echo "[$(date +%H:%M:%S)] $*"; }
die(){ echo "❌ $*" >&2; echo "RUN_DIR=$RUN_DIR" >&2; exit 1; }

curl_to_file() {
  # METHOD URL OUTFILE [BODYFILE]
  local method="$1"; shift
  local url="$1"; shift
  local outfile="$1"; shift
  local bodyfile="${1:-}"
  if [ -n "$bodyfile" ]; then
    curl -sS -X "$method" "$url" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "X-User-Id: ${X_USER_ID}" \
      -H "Content-Type: application/json" \
      -d @"$bodyfile" \
      -o "$outfile" -w "%{http_code}"
  else
    curl -sS -X "$method" "$url" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "X-User-Id: ${X_USER_ID}" \
      -o "$outfile" -w "%{http_code}"
  fi
}

assert_code() {
  local got="$1"; local want="$2"; local ctx="$3"; local bodyfile="$4"
  if [ "$got" != "$want" ]; then
    echo "---- $ctx body ($got) ----" >&2
    cat "$bodyfile" >&2 || true
    echo "--------------------------" >&2
    die "$ctx expected HTTP $want, got $got"
  fi
}

auto_detect_saree() {
  if [ -z "$SAREE_FILE" ]; then
    local latest
    latest="$(ls -td /tmp/saree_eval_* 2>/dev/null | head -n 1 || true)"
    if [ -n "$latest" ] && [ -f "$latest/saree.png" ]; then
      SAREE_FILE="$latest/saree.png"
    fi
  fi
  [ -n "$SAREE_FILE" ] && [ -f "$SAREE_FILE" ] || die "SAREE_FILE not found. Set SAREE_FILE=/absolute/path/to/saree.png"
  log "SAREE_FILE=$SAREE_FILE"
}

decode_jwt_sub() {
python3 - <<'PY'
import os,base64,json
tok=os.environ.get("TOKEN","")
p=tok.split(".")
if len(p)<2:
  print("")
  raise SystemExit(0)
payload=p[1] + "="*((4-len(p[1])%4)%4)
data=json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
print(data.get("sub",""))
PY
}

login() {
  log "Logging in to svc-core..."
  curl -sS -X POST "${CORE_URL}/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{
      \"email\": \"${EMAIL}\",
      \"password\": \"${PASSWORD}\",
      \"device_id\": \"${DEVICE_ID}\",
      \"client_type\": \"${CLIENT_TYPE}\"
    }" > "$RUN_DIR/auth.json"

  TOKEN="$(jq -r '.access_token // .token // .bearer_token // .jwt // empty' "$RUN_DIR/auth.json")"
  X_USER_ID="$(jq -r '.user_id // .x_user_id // .user.id // .user.user_id // .id // empty' "$RUN_DIR/auth.json")"

  [ -n "$TOKEN" ] || die "No token in login response. See $RUN_DIR/auth.json"
  if [ -z "$X_USER_ID" ]; then
    X_USER_ID="$(TOKEN="$TOKEN" decode_jwt_sub)"
  fi
  [ -n "$X_USER_ID" ] || die "No user id found in login response and JWT sub decode failed. See $RUN_DIR/auth.json"

  log "Auth OK. X_USER_ID=$X_USER_ID"
}

check_openapi() {
  log "Checking OpenAPI has assets route..."
  curl -sS "${COMMERCE_URL}/openapi.json" > "$RUN_DIR/openapi.json"
  jq -r '.paths | keys[]' "$RUN_DIR/openapi.json" | grep -q "/api/commerce/assets/upload" \
    || die "assets endpoint missing from OpenAPI. Fix router registration."
  log "OpenAPI OK."
}

upload_saree() {
  log "Uploading saree_full..."
  curl -sS -X POST "${COMMERCE_URL}/api/commerce/assets/upload" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-User-Id: ${X_USER_ID}" \
    -F "role=saree_full" \
    -F "owner_type=merchant" \
    -F "file=@${SAREE_FILE}" > "$RUN_DIR/upload_saree.json"

  jq . "$RUN_DIR/upload_saree.json" >/dev/null 2>&1 || die "Upload response not JSON: $RUN_DIR/upload_saree.json"
  SAREE_URL="$(jq -r '.preview_url // empty' "$RUN_DIR/upload_saree.json")"
  [ -n "$SAREE_URL" ] || die "Missing preview_url from upload. See $RUN_DIR/upload_saree.json"
  log "Upload OK."
}

_extract_urls() {
  # prints urls one per line
  jq -r '(.urls // .computed.urls // [])[]' "$1" 2>/dev/null || true
}

_validate_url_head() {
  local u="$1"
  # IMPORTANT: SAS URLs have &; always quote.
  curl -fsSIL "$u" --max-time "$HEAD_TIMEOUT_SECS" >/dev/null 2>&1
}

vendor_only_flow() {
  log "Quote (vendor-only): platform_models, no model_ref (backend auto-picks model)..."
  jq -n --arg saree "$SAREE_URL" '{
    mode:"platform_models",
    product_type:"apparel",
    resolution:"hd",
    outputs:{num_images:4,num_videos:0},
    views:{full_body:true,half_body:false},
    drape_styles:["nivi"],
    product_assets:{
      saree_image_url:$saree,
      items:[{component_code:"saree",kind:"garment",image_url:$saree,image_urls:[$saree],is_primary:true}]
    }
  }' > "$RUN_DIR/quote_body.json"

  code="$(curl_to_file POST "${COMMERCE_URL}/api/commerce/quote" "$RUN_DIR/quote_out.json" "$RUN_DIR/quote_body.json")"
  assert_code "$code" "200" "quote" "$RUN_DIR/quote_out.json"
  QUOTE_ID="$(jq -r '.quote_id // empty' "$RUN_DIR/quote_out.json")"
  [ -n "$QUOTE_ID" ] || die "quote_id missing. See $RUN_DIR/quote_out.json"
  log "Quote OK: $QUOTE_ID"

  jq -n --arg q "$QUOTE_ID" '{quote_id:$q}' > "$RUN_DIR/confirm_body.json"
  code="$(curl_to_file POST "${COMMERCE_URL}/api/commerce/confirm" "$RUN_DIR/confirm_out.json" "$RUN_DIR/confirm_body.json")"
  assert_code "$code" "200" "confirm" "$RUN_DIR/confirm_out.json"
  JOB_ID="$(jq -r '.studio_job_id // empty' "$RUN_DIR/confirm_out.json")"
  [ -n "$JOB_ID" ] || die "studio_job_id missing. See $RUN_DIR/confirm_out.json"
  log "Job queued: $JOB_ID"

  log "Polling status..."
  deadline=$(( $(date +%s) + POLL_TIMEOUT_SECS ))
  while true; do
    code="$(curl_to_file GET "${COMMERCE_URL}/api/commerce/jobs/${JOB_ID}/status?include_payload=1" "$RUN_DIR/status.json")"
    assert_code "$code" "200" "status" "$RUN_DIR/status.json"
    st="$(jq -r '.status // empty' "$RUN_DIR/status.json")"
    log "status=${st:-?}"
    if [ "$st" = "succeeded" ] || [ "$st" = "failed" ]; then break; fi
    [ "$(date +%s)" -lt "$deadline" ] || die "timeout waiting; see $RUN_DIR/status.json"
    sleep "$POLL_SECS"
  done

  if [ "$(jq -r '.status' "$RUN_DIR/status.json")" != "succeeded" ]; then
    die "job failed. See $RUN_DIR/status.json"
  fi

  # Robust urls extraction (supports both top-level urls and computed.urls)
  URLS="$(_extract_urls "$RUN_DIR/status.json")"
  if [ -z "$URLS" ]; then
    echo "---- status.json (snippet) ----" >&2
    jq '{status, stage, urls, computed_urls:(.computed.urls // null), computed_keys:(.computed|keys)}' "$RUN_DIR/status.json" >&2 || true
    echo "------------------------------" >&2
    die "succeeded but urls empty. See $RUN_DIR/status.json"
  fi

  FIRST_URL="$(printf "%s\n" "$URLS" | head -n 1)"
  [ -n "$FIRST_URL" ] || die "unexpected: extracted urls empty after non-empty check"

  if [ "$VALIDATE_URLS" = "1" ]; then
    log "Validating first URL via HEAD (quoted SAS URL)..."
    if ! _validate_url_head "$FIRST_URL"; then
      echo "FIRST_URL=$FIRST_URL" >&2
      die "first output URL HEAD failed (check SAS/permissions)."
    fi
  fi

  log "✅ SUCCESS"
  log "Preview URL: ${FIRST_URL}"
  log "URLs (first 4):"
  printf "%s\n" "$URLS" | head -n 4
}

log "START vendor-only E2E"
auto_detect_saree
login
check_openapi
upload_saree
vendor_only_flow

echo
echo "✅ DONE"
echo "RUN_DIR=$RUN_DIR"