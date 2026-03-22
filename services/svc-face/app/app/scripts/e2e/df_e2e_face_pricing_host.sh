#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# DesiFaces Face Studio Pricing E2E
#
# Flow:
#   1) login via svc-core
#   2) pricing preview via svc-face
#   3) generate via svc-face with pricing_confirmation
#   4) poll job status until terminal
#   5) print terminal pricing + pricing_summary
#
# Host URLs requested by user:
#   FACE_URL=http://localhost:8003
#   CORE_URL=http://localhost:8000
#
# Test user requested by user:
#   DF_EMAIL=user2@desifaces.ai
#   DF_PASSWORD=password2
#
# Prompt requested by user:
#   "A college female student from assam in jeans and t-shirt with backpack"
# ============================================================================

export FACE_URL="${FACE_URL:-http://localhost:8003}"
export CORE_URL="${CORE_URL:-http://localhost:8000}"
export DF_EMAIL="${DF_EMAIL:-user2@desifaces.ai}"
export DF_PASSWORD="${DF_PASSWORD:-password2}"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${OUT_DIR:-/tmp/df_e2e_face_pricing_${RUN_TS}}"
mkdir -p "$OUT_DIR"

AUTH_JSON="$OUT_DIR/auth.json"
PREVIEW_REQ_JSON="$OUT_DIR/preview_request.json"
PREVIEW_RESP_JSON="$OUT_DIR/preview_response.json"
GENERATE_REQ_JSON="$OUT_DIR/generate_request.json"
GENERATE_RESP_JSON="$OUT_DIR/generate_response.json"
STATUS_JSON="$OUT_DIR/status.json"
SUMMARY_JSON="$OUT_DIR/summary.json"

echo "OUT_DIR=$OUT_DIR"
echo "FACE_URL=$FACE_URL"
echo "CORE_URL=$CORE_URL"
echo "DF_EMAIL=$DF_EMAIL"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $1" >&2
    exit 1
  }
}

require_cmd curl
require_cmd python3

json_get() {
  local file="$1"
  local expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json, sys
path = sys.argv[1]
expr = sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

cur = data
for part in expr.split("."):
    if not part:
        continue
    if isinstance(cur, dict):
        cur = cur.get(part)
    else:
        cur = None
        break

if cur is None:
    print("")
elif isinstance(cur, (dict, list)):
    import json as _json
    print(_json.dumps(cur, ensure_ascii=False))
else:
    print(str(cur))
PY
}

pretty_json() {
  local file="$1"
  python3 - "$file" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    print(json.dumps(json.load(f), indent=2, ensure_ascii=False))
PY
}

decode_user_id_from_jwt() {
  local jwt="$1"
  python3 - "$jwt" <<'PY'
import base64, json, sys
tok = sys.argv[1].strip()
if not tok:
    print("")
    raise SystemExit(0)

parts = tok.split(".")
if len(parts) < 2:
    print("")
    raise SystemExit(0)

payload = parts[1]
payload += "=" * (-len(payload) % 4)
try:
    data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
except Exception:
    print("")
    raise SystemExit(0)

print(data.get("sub") or data.get("user_id") or "")
PY
}

http_post_json() {
  local url="$1"
  local payload_file="$2"
  local output_file="$3"
  shift 3
  curl -sS -X POST "$url" \
    -H "Content-Type: application/json" \
    "$@" \
    --data @"$payload_file" \
    > "$output_file"
}

http_get_json() {
  local url="$1"
  local output_file="$2"
  shift 2
  curl -sS "$url" "$@" > "$output_file"
}

echo
echo "==> 1) Login via svc-core"

cat > "$OUT_DIR/login_request.json" <<JSON
{
  "email": "${DF_EMAIL}",
  "password": "${DF_PASSWORD}"
}
JSON

http_post_json \
  "$CORE_URL/api/auth/login" \
  "$OUT_DIR/login_request.json" \
  "$AUTH_JSON"

echo "Login response:"
pretty_json "$AUTH_JSON"

TOKEN="$(json_get "$AUTH_JSON" "access_token")"
if [[ -z "$TOKEN" ]]; then
  TOKEN="$(json_get "$AUTH_JSON" "token")"
fi

USER_ID="$(json_get "$AUTH_JSON" "user_id")"
if [[ -z "$USER_ID" ]]; then
  USER_ID="$(decode_user_id_from_jwt "$TOKEN")"
fi

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: access token not found in login response" >&2
  exit 1
fi

if [[ -z "$USER_ID" ]]; then
  echo "ERROR: user_id not found in login response or JWT" >&2
  exit 1
fi

echo
echo "Resolved auth:"
echo "  USER_ID=$USER_ID"
echo "  TOKEN_LENGTH=${#TOKEN}"

echo
echo "==> 2) Build pricing preview request"

cat > "$PREVIEW_REQ_JSON" <<'JSON'
{
  "mode": "t2i",
  "count": 2,
  "studio_input": {
    "user_prompt": "A college female student from assam in jeans and t-shirt with backpack"
  }
}
JSON

echo "Preview request:"
pretty_json "$PREVIEW_REQ_JSON"

echo
echo "==> 3) Pricing preview"

http_post_json \
  "$FACE_URL/api/face/creator/pricing/preview" \
  "$PREVIEW_REQ_JSON" \
  "$PREVIEW_RESP_JSON" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID"

echo "Preview response:"
pretty_json "$PREVIEW_RESP_JSON"

QUOTE_ID="$(json_get "$PREVIEW_RESP_JSON" "quote_id")"
PREVIEW_FINGERPRINT="$(json_get "$PREVIEW_RESP_JSON" "preview_fingerprint")"

if [[ -z "$QUOTE_ID" ]]; then
  echo "ERROR: quote_id missing from pricing preview response" >&2
  exit 1
fi

if [[ -z "$PREVIEW_FINGERPRINT" ]]; then
  echo "ERROR: preview_fingerprint missing from pricing preview response" >&2
  exit 1
fi

echo
echo "Resolved preview confirmation:"
echo "  QUOTE_ID=$QUOTE_ID"
echo "  PREVIEW_FINGERPRINT=$PREVIEW_FINGERPRINT"

echo
echo "==> 4) Build generate request with pricing_confirmation"

python3 - "$PREVIEW_REQ_JSON" "$QUOTE_ID" "$PREVIEW_FINGERPRINT" > "$GENERATE_REQ_JSON" <<'PY'
import json, sys

preview_req_path = sys.argv[1]
quote_id = sys.argv[2]
preview_fingerprint = sys.argv[3]

with open(preview_req_path, "r", encoding="utf-8") as f:
    base = json.load(f)

base["pricing_confirmation"] = {
    "quote_id": quote_id,
    "preview_fingerprint": preview_fingerprint,
}

print(json.dumps(base, ensure_ascii=False, indent=2))
PY

echo "Generate request:"
pretty_json "$GENERATE_REQ_JSON"

echo
echo "==> 5) Generate job"

http_post_json \
  "$FACE_URL/api/face/creator/generate" \
  "$GENERATE_REQ_JSON" \
  "$GENERATE_RESP_JSON" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID"

echo "Generate response:"
pretty_json "$GENERATE_RESP_JSON"

JOB_ID="$(json_get "$GENERATE_RESP_JSON" "job_id")"
if [[ -z "$JOB_ID" ]]; then
  JOB_ID="$(json_get "$GENERATE_RESP_JSON" "id")"
fi

if [[ -z "$JOB_ID" ]]; then
  echo "ERROR: job_id missing from generate response" >&2
  exit 1
fi

echo
echo "Resolved job:"
echo "  JOB_ID=$JOB_ID"

echo
echo "==> 6) Poll status until terminal"

MAX_POLLS="${MAX_POLLS:-120}"
SLEEP_SECS="${SLEEP_SECS:-3}"

FINAL_STATUS=""
FINAL_PRICING_STATE=""

for ((i=1; i<=MAX_POLLS; i++)); do
  http_get_json \
    "$FACE_URL/api/face/creator/jobs/$JOB_ID/status" \
    "$STATUS_JSON" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-User-Id: $USER_ID"

  JOB_STATUS="$(json_get "$STATUS_JSON" "status")"
  if [[ -z "$JOB_STATUS" ]]; then
    JOB_STATUS="$(json_get "$STATUS_JSON" "stage")"
  fi

  PRICING_STATE="$(json_get "$STATUS_JSON" "pricing.state")"

  echo "poll=$i status=${JOB_STATUS:-<empty>} pricing_state=${PRICING_STATE:-<empty>}"

  if [[ "$JOB_STATUS" == "succeeded" || "$JOB_STATUS" == "failed" || "$JOB_STATUS" == "cancelled" ]]; then
    FINAL_STATUS="$JOB_STATUS"
    FINAL_PRICING_STATE="$PRICING_STATE"
    break
  fi

  sleep "$SLEEP_SECS"
done

if [[ -z "$FINAL_STATUS" ]]; then
  echo "ERROR: job did not reach terminal state within polling window" >&2
  echo "Last observed status payload:"
  pretty_json "$STATUS_JSON"
  exit 1
fi

echo
echo "Final status payload:"
pretty_json "$STATUS_JSON"

echo
echo "==> 7) Build summary"

python3 - "$STATUS_JSON" "$PREVIEW_RESP_JSON" "$GENERATE_RESP_JSON" "$JOB_ID" "$QUOTE_ID" "$PREVIEW_FINGERPRINT" "$SUMMARY_JSON" <<'PY'
import json, sys

status_path = sys.argv[1]
preview_path = sys.argv[2]
generate_path = sys.argv[3]
job_id = sys.argv[4]
quote_id = sys.argv[5]
preview_fingerprint = sys.argv[6]
summary_path = sys.argv[7]

with open(status_path, "r", encoding="utf-8") as f:
    status = json.load(f)
with open(preview_path, "r", encoding="utf-8") as f:
    preview = json.load(f)
with open(generate_path, "r", encoding="utf-8") as f:
    generate = json.load(f)

pricing = status.get("pricing") or {}
pricing_summary = status.get("pricing_summary") or {}

summary = {
    "job_id": job_id,
    "quote_id": quote_id,
    "preview_fingerprint": preview_fingerprint,
    "job_status": status.get("status") or status.get("stage"),
    "pricing_state": pricing.get("state"),
    "billing_mode": pricing.get("billing_mode"),
    "settlement_mode": pricing.get("settlement_mode"),
    "sku_code": pricing.get("sku_code"),
    "amount": pricing.get("amount"),
    "currency": pricing.get("currency"),
    "actual_units": pricing.get("actual_units"),
    "billed_units": pricing.get("billed_units"),
    "reservation_id": pricing.get("reservation_id"),
    "summary_estimated_amount": pricing_summary.get("estimated_amount"),
    "summary_final_amount": pricing_summary.get("final_amount"),
    "summary_delta_amount": pricing_summary.get("delta_amount"),
    "preview_response": preview,
    "generate_response": generate,
    "terminal_status_response": status,
}

with open(summary_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)

print(json.dumps(summary, indent=2, ensure_ascii=False))
PY

echo
echo "Summary saved to:"
echo "  $SUMMARY_JSON"

echo
echo "==> 8) Pass/fail guidance"
echo "Expected terminal pricing behavior:"
echo "  - if job_status=succeeded  => pricing_state should be committed"
echo "  - if job_status=failed     => pricing_state should be released"
echo "  - if job_status=cancelled  => pricing_state should usually be released"

if [[ "$FINAL_STATUS" == "succeeded" && "$FINAL_PRICING_STATE" != "committed" ]]; then
  echo
  echo "WARNING: job succeeded but pricing_state is '$FINAL_PRICING_STATE' instead of 'committed'"
  exit 2
fi

if [[ "$FINAL_STATUS" == "failed" && "$FINAL_PRICING_STATE" != "released" ]]; then
  echo
  echo "WARNING: job failed but pricing_state is '$FINAL_PRICING_STATE' instead of 'released'"
  exit 3
fi

if [[ "$FINAL_STATUS" == "cancelled" && "$FINAL_PRICING_STATE" != "released" ]]; then
  echo
  echo "WARNING: job cancelled but pricing_state is '$FINAL_PRICING_STATE' instead of 'released'"
  exit 4
fi

echo
echo "E2E completed successfully."