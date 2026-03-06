#!/usr/bin/env bash
set -euo pipefail

# -----------------------------
# Config (edit via env vars)
# -----------------------------
CORE_URL="${CORE_URL:-http://localhost:8000}"
COMMERCE_URL="${COMMERCE_URL:-http://localhost:8008}"

EMAIL="${EMAIL:-df_service_account_new@desifaces.ai}"
PASSWORD="${PASSWORD:-desifaces_mahadev_password}"
DEVICE_ID="${DEVICE_ID:-mobile}"
CLIENT_TYPE="${CLIENT_TYPE:-ios}"

# Required garment inputs (local file paths)
SAREE_FILE="${SAREE_FILE:-}"
PALLU_FILE="${PALLU_FILE:-}"
BORDER_FILE="${BORDER_FILE:-}"

# Optional: user photo for customer try-on (local file path)
PERSON_FILE="${PERSON_FILE:-}"

# Optional: if you already have a URL for the person photo, provide it
MODEL_IMAGE_URL="${MODEL_IMAGE_URL:-}"

# VTON controls
MODE="${MODE:-customer_tryon}"          # customer_tryon | platform_models
PRODUCT_TYPE="${PRODUCT_TYPE:-apparel}"
OUTFIT_KIND="${OUTFIT_KIND:-saree_set}"
DRAPE_STYLE="${DRAPE_STYLE:-nivi}"
RESOLUTION="${RESOLUTION:-hd}"
NUM_IMAGES="${NUM_IMAGES:-4}"

# Product pack metadata
PRODUCT_TITLE="${PRODUCT_TITLE:-Demo Saree Pack $(date +%Y%m%d_%H%M%S)}"
PRODUCT_CATEGORY="${PRODUCT_CATEGORY:-apparel}"
PRODUCT_SKU="${PRODUCT_SKU:-}"

# Polling
POLL_SECS="${POLL_SECS:-5}"
POLL_TIMEOUT_SECS="${POLL_TIMEOUT_SECS:-600}"

# -----------------------------
# Helpers
# -----------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1"; exit 2; }; }
need curl
need jq
need python3

RUN_DIR="/tmp/df_commerce_demo_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RUN_DIR"

log() { echo "[$(date +%H:%M:%S)] $*"; }

json_get() { jq -r "$1 // empty" <<<"$2"; }

jwt_sub() {
python3 - <<'PY'
import os,base64,json
t=os.environ.get("TOKEN","")
p=t.split(".")
if len(p)<2:
    print("")
    raise SystemExit(0)
payload=p[1]+"="*((4-len(p[1])%4)%4)
data=json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
print(data.get("sub",""))
PY
}

auth_headers() {
  echo -H "Authorization: Bearer ${TOKEN}" -H "X-User-Id: ${X_USER_ID}"
}

# -----------------------------
# 1) Login
# -----------------------------
log "Logging in to svc-core..."
AUTH_JSON="$(curl -sS -X POST "${CORE_URL}/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{
    \"email\": \"${EMAIL}\",
    \"password\": \"${PASSWORD}\",
    \"device_id\": \"${DEVICE_ID}\",
    \"client_type\": \"${CLIENT_TYPE}\"
  }")"

echo "$AUTH_JSON" | jq . > "$RUN_DIR/auth.json"

TOKEN="$(json_get '.access_token' "$AUTH_JSON")"
if [ -z "$TOKEN" ]; then TOKEN="$(json_get '.token' "$AUTH_JSON")"; fi
if [ -z "$TOKEN" ]; then TOKEN="$(json_get '.bearer_token' "$AUTH_JSON")"; fi

X_USER_ID="$(json_get '.user_id' "$AUTH_JSON")"
if [ -z "$X_USER_ID" ]; then X_USER_ID="$(json_get '.x_user_id' "$AUTH_JSON")"; fi
if [ -z "$X_USER_ID" ] && [ -n "$TOKEN" ]; then
  X_USER_ID="$(TOKEN="$TOKEN" jwt_sub)"
fi

if [ -z "$TOKEN" ] || [ -z "$X_USER_ID" ]; then
  echo "Login failed or unexpected response. See $RUN_DIR/auth.json"
  exit 1
fi

log "Auth OK. X_USER_ID=$X_USER_ID"
log "RUN_DIR=$RUN_DIR"

# -----------------------------
# 2) Upload helper
# -----------------------------
upload_asset() {
  local role="$1"
  local owner_type="$2"
  local file_path="$3"

  if [ -z "$file_path" ] || [ ! -f "$file_path" ]; then
    echo ""
    return 0
  fi

  log "Uploading asset role=$role file=$file_path"
  local resp
  resp="$(curl -sS -X POST "${COMMERCE_URL}/api/commerce/assets/upload" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-User-Id: ${X_USER_ID}" \
    -F "role=${role}" \
    -F "owner_type=${owner_type}" \
    -F "file=@${file_path}")"

  echo "$resp" | jq . > "$RUN_DIR/upload_${role}.json"

  local asset_id preview_url
  asset_id="$(json_get '.asset_id' "$resp")"
  preview_url="$(json_get '.preview_url' "$resp")"

  if [ -z "$asset_id" ] || [ -z "$preview_url" ]; then
    echo "Upload failed for role=$role. See $RUN_DIR/upload_${role}.json"
    exit 1
  fi

  echo "${asset_id}|${preview_url}"
}

# -----------------------------
# 3) Upload garment assets (required)
# -----------------------------
if [ -z "$SAREE_FILE" ] || [ ! -f "$SAREE_FILE" ]; then
  echo "Set SAREE_FILE=/path/to/saree.png"
  exit 2
fi
if [ -z "$PALLU_FILE" ] || [ ! -f "$PALLU_FILE" ]; then
  echo "Set PALLU_FILE=/path/to/pallu.png"
  exit 2
fi
if [ -z "$BORDER_FILE" ] || [ ! -f "$BORDER_FILE" ]; then
  echo "Set BORDER_FILE=/path/to/border.png"
  exit 2
fi

SAREE_UP="$(upload_asset "saree_full" "merchant" "$SAREE_FILE")"
PALLU_UP="$(upload_asset "pallu_full" "merchant" "$PALLU_FILE")"
BORDER_UP="$(upload_asset "border_closeup" "merchant" "$BORDER_FILE")"

SAREE_ASSET_ID="${SAREE_UP%%|*}"; SAREE_URL="${SAREE_UP#*|}"
PALLU_ASSET_ID="${PALLU_UP%%|*}"; PALLU_URL="${PALLU_UP#*|}"
BORDER_ASSET_ID="${BORDER_UP%%|*}"; BORDER_URL="${BORDER_UP#*|}"

log "Uploaded garment assets:"
log "  saree_full:  $SAREE_ASSET_ID"
log "  pallu_full:  $PALLU_ASSET_ID"
log "  border:      $BORDER_ASSET_ID"

# -----------------------------
# 4) Optional: upload person photo OR use MODEL_IMAGE_URL
# -----------------------------
if [ -n "$PERSON_FILE" ] && [ -f "$PERSON_FILE" ]; then
  PERSON_UP="$(upload_asset "person_full_body" "consumer" "$PERSON_FILE")"
  PERSON_ASSET_ID="${PERSON_UP%%|*}"; PERSON_URL="${PERSON_UP#*|}"
  MODEL_IMAGE_URL="$PERSON_URL"
  log "Uploaded person photo asset_id=$PERSON_ASSET_ID"
fi

if [ -z "$MODEL_IMAGE_URL" ]; then
  echo "Provide PERSON_FILE=/path/to/person.jpg OR MODEL_IMAGE_URL=https://..."
  exit 2
fi

# -----------------------------
# 5) Create merchant product pack
# -----------------------------
log "Creating merchant product pack..."
CREATE_BODY="$(jq -n \
  --arg category "$PRODUCT_CATEGORY" \
  --arg title "$PRODUCT_TITLE" \
  --arg sku "$PRODUCT_SKU" \
  --arg outfit_kind "$OUTFIT_KIND" \
  --arg drape "$DRAPE_STYLE" \
  --arg saree_id "$SAREE_ASSET_ID" \
  --arg pallu_id "$PALLU_ASSET_ID" \
  --arg border_id "$BORDER_ASSET_ID" \
  '{
    category: $category,
    title: $title,
    sku: (if $sku=="" then null else $sku end),
    outfit_kind: $outfit_kind,
    default_drape_style: $drape,
    metadata: { outfit_kind: $outfit_kind, default_drape_style: $drape },
    assets: [
      {role:"saree_full", asset_id:$saree_id, optional:false},
      {role:"pallu_full", asset_id:$pallu_id, optional:false},
      {role:"border_closeup", asset_id:$border_id, optional:false}
    ]
  }')"

echo "$CREATE_BODY" > "$RUN_DIR/product_create_body.json"

CREATE_RESP="$(curl -sS -X POST "${COMMERCE_URL}/api/commerce/merchants/${X_USER_ID}/products" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-User-Id: ${X_USER_ID}" \
  -H "Content-Type: application/json" \
  -d @"$RUN_DIR/product_create_body.json")"

echo "$CREATE_RESP" | jq . > "$RUN_DIR/product_create_resp.json"
PRODUCT_ID="$(json_get '.product_id' "$CREATE_RESP")"

if [ -z "$PRODUCT_ID" ]; then
  echo "Product creation failed. See $RUN_DIR/product_create_resp.json"
  exit 1
fi
log "Product created: $PRODUCT_ID"

# -----------------------------
# 6) Validate + publish product
# -----------------------------
log "Validating product..."
VAL_RESP="$(curl -sS -X POST "${COMMERCE_URL}/api/commerce/products/${PRODUCT_ID}/validate" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-User-Id: ${X_USER_ID}")"
echo "$VAL_RESP" | jq . > "$RUN_DIR/product_validate.json"

OK="$(json_get '.ok' "$VAL_RESP")"
if [ "$OK" != "true" ]; then
  echo "Product validation failed. See $RUN_DIR/product_validate.json"
  exit 1
fi
log "Product validated."

log "Publishing product..."
PUB_RESP="$(curl -sS -X POST "${COMMERCE_URL}/api/commerce/products/${PRODUCT_ID}/publish" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-User-Id: ${X_USER_ID}")"
echo "$PUB_RESP" | jq . > "$RUN_DIR/product_publish.json"
log "Product published."

# -----------------------------
# 7) Quote -> Confirm -> Poll Status
# -----------------------------
log "Creating quote..."
QUOTE_BODY="$(jq -n \
  --arg mode "$MODE" \
  --arg product_type "$PRODUCT_TYPE" \
  --arg resolution "$RESOLUTION" \
  --arg model_url "$MODEL_IMAGE_URL" \
  --arg product_id "$PRODUCT_ID" \
  --arg drape "$DRAPE_STYLE" \
  --arg saree_url "$SAREE_URL" \
  --arg pallu_url "$PALLU_URL" \
  --arg border_url "$BORDER_URL" \
  --argjson num_images "$NUM_IMAGES" \
  '{
    mode: $mode,
    product_type: $product_type,
    resolution: $resolution,
    product_ids: [$product_id],
    outputs: {num_images: $num_images, num_videos: 0},
    views: {full_body: true, half_body: false},
    drape_styles: [$drape],
    model_ref: { image_url: $model_url },
    product_assets: {
      items: [
        {component_code:"saree", kind:"garment", image_url: $saree_url, is_primary: true,
         meta: {role:"saree_full", pallu_url:$pallu_url, border_url:$border_url}
        }
      ],
      saree_image_url: $saree_url,
      meta: {pallu_url:$pallu_url, border_url:$border_url}
    }
  }')"

echo "$QUOTE_BODY" > "$RUN_DIR/quote_body.json"

QUOTE_RESP="$(curl -sS -X POST "${COMMERCE_URL}/api/commerce/quote" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-User-Id: ${X_USER_ID}" \
  -H "Content-Type: application/json" \
  -d @"$RUN_DIR/quote_body.json")"

echo "$QUOTE_RESP" | jq . > "$RUN_DIR/quote_resp.json"
QUOTE_ID="$(json_get '.quote_id' "$QUOTE_RESP")"
if [ -z "$QUOTE_ID" ]; then
  echo "Quote failed. See $RUN_DIR/quote_resp.json and $RUN_DIR/quote_body.json"
  exit 1
fi
log "Quote OK: $QUOTE_ID"

log "Confirming..."
CONFIRM_BODY="$(jq -n --arg q "$QUOTE_ID" '{quote_id:$q}')"
echo "$CONFIRM_BODY" > "$RUN_DIR/confirm_body.json"

CONFIRM_RESP="$(curl -sS -X POST "${COMMERCE_URL}/api/commerce/confirm" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "X-User-Id: ${X_USER_ID}" \
  -H "Content-Type: application/json" \
  -d @"$RUN_DIR/confirm_body.json")"

echo "$CONFIRM_RESP" | jq . > "$RUN_DIR/confirm_resp.json"
JOB_ID="$(json_get '.studio_job_id' "$CONFIRM_RESP")"
if [ -z "$JOB_ID" ]; then
  echo "Confirm failed. See $RUN_DIR/confirm_resp.json"
  exit 1
fi
log "Job started: $JOB_ID"

log "Polling status..."
deadline=$(( $(date +%s) + POLL_TIMEOUT_SECS ))
while true; do
  STATUS_RESP="$(curl -sS -X GET "${COMMERCE_URL}/api/commerce/jobs/${JOB_ID}/status?include_payload=1" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-User-Id: ${X_USER_ID}")"
  echo "$STATUS_RESP" | jq . > "$RUN_DIR/status_latest.json"

  status="$(json_get '.status' "$STATUS_RESP")"
  stage="$(json_get '.stage' "$STATUS_RESP")"

  log "status=${status:-?} stage=${stage:-?}"

  if [[ "$status" == "succeeded" || "$status" == "failed" ]]; then
    log "DONE status=$status"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Timed out waiting for job. See $RUN_DIR/status_latest.json"
    exit 1
  fi
  sleep "$POLL_SECS"
done

echo
echo "✅ Demo run complete."
echo "RUN_DIR=$RUN_DIR"
echo "Open latest status: $RUN_DIR/status_latest.json"