#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# DesiFaces svc-fusion pricing E2E
#
# Features:
# - Logs in via svc-core
# - Auto-fetches latest successful Face and Audio artifact IDs from desifaces-db
#   for the logged-in user if FACE_ARTIFACT_ID / AUDIO_ARTIFACT_ID are unset
# - Uses the live Fusion routes:
#     POST /jobs/pricing/preview
#     POST /jobs
#     GET  /jobs/{job_id}
# - Verifies pricing terminal correctness:
#     succeeded -> committed
#     failed/canceled/cancelled -> released
#
# Environment variables:
#   CORE_URL         default: http://localhost:8000
#   FUSION_URL       default: http://localhost:8002
#   DF_EMAIL         default: user2@desifaces.ai
#   DF_PASSWORD      default: password2
#   FACE_ARTIFACT_ID optional
#   AUDIO_ARTIFACT_ID optional
#   FACE_URL_INPUT   optional
#   AUDIO_URL_INPUT  optional
#   MAX_POLLS        default: 240
#   POLL_SECS        default: 5
# -----------------------------------------------------------------------------

export CORE_URL="${CORE_URL:-http://localhost:8000}"
export FUSION_URL="${FUSION_URL:-http://localhost:8002}"
export DF_EMAIL="${DF_EMAIL:-user2@desifaces.ai}"
export DF_PASSWORD="${DF_PASSWORD:-password2}"
export MAX_POLLS="${MAX_POLLS:-240}"
export POLL_SECS="${POLL_SECS:-5}"

OUT_DIR="${OUT_DIR:-/tmp/df_e2e_fusion_with_pricing_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

AUTH_JSON="$OUT_DIR/auth.json"
PREVIEW_REQ="$OUT_DIR/fusion_preview_req.json"
PREVIEW_RESP="$OUT_DIR/fusion_preview_resp.json"
GENERATE_REQ="$OUT_DIR/fusion_generate_req.json"
GENERATE_RESP="$OUT_DIR/fusion_generate_resp.json"
STATUS_JSON="$OUT_DIR/fusion_status.json"
SUMMARY_JSON="$OUT_DIR/summary.json"

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
path, expr = sys.argv[1], sys.argv[2]
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
    print(json.dumps(cur, ensure_ascii=False))
else:
    print(str(cur))
PY
}

pretty() { python3 -m json.tool "$1"; }

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
payload = parts[1] + "=" * (-len(parts[1]) % 4)
try:
    data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
except Exception:
    print("")
    raise SystemExit(0)
print(data.get("sub") or data.get("user_id") or "")
PY
}

post_json_capture() {
  local url="$1"; local payload="$2"; local output="$3"; local headers_out="$4"; shift 4
  curl -sS -D "$headers_out" -o "$output" -X POST "$url" -H "Content-Type: application/json" "$@" --data @"$payload"
}

get_json_capture() {
  local url="$1"; local output="$2"; local headers_out="$3"; shift 3
  curl -sS -D "$headers_out" -o "$output" "$url" "$@"
}

http_status_from_headers() {
  local headers_file="$1"
  python3 - "$headers_file" <<'PY'
import sys
status = ""
with open(sys.argv[1], "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if line.startswith("HTTP/"):
            parts = line.strip().split()
            if len(parts) >= 2:
                status = parts[1]
print(status)
PY
}

docker_db_query_single() {
  local sql="$1"
  docker exec -i desifaces-db bash -lc "psql -U \"\$POSTGRES_USER\" -d \"\${POSTGRES_DB:-postgres}\" -At -c \"$sql\"" 2>/dev/null || true
}

echo "OUT_DIR=$OUT_DIR"
echo "CORE_URL=$CORE_URL"
echo "FUSION_URL=$FUSION_URL"
echo "DF_EMAIL=$DF_EMAIL"

cat > "$OUT_DIR/login_request.json" <<JSON
{"email":"$DF_EMAIL","password":"$DF_PASSWORD"}
JSON

echo
echo "==> Login"
curl -sS -X POST "$CORE_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  --data @"$OUT_DIR/login_request.json" \
  > "$AUTH_JSON"
pretty "$AUTH_JSON"

TOKEN="$(json_get "$AUTH_JSON" "access_token")"
if [[ -z "$TOKEN" ]]; then TOKEN="$(json_get "$AUTH_JSON" "token")"; fi
USER_ID="$(json_get "$AUTH_JSON" "user_id")"
if [[ -z "$USER_ID" ]]; then USER_ID="$(decode_user_id_from_jwt "$TOKEN")"; fi

if [[ -z "$TOKEN" || -z "$USER_ID" ]]; then
  echo "ERROR: token/user_id missing" >&2
  exit 1
fi
echo "USER_ID=$USER_ID"

# Auto-resolve latest successful artifact IDs from DB when not provided.
if [[ -z "${FACE_ARTIFACT_ID:-}" && -z "${FACE_URL_INPUT:-}" ]]; then
  FACE_ARTIFACT_ID="$(
    docker_db_query_single "SELECT a.id::text
FROM public.artifacts a
JOIN public.studio_jobs j ON j.id = a.job_id
WHERE j.user_id = '$USER_ID'
  AND j.studio_type = 'face'
  AND j.status = 'succeeded'
  AND a.kind IN ('image','face','face_image')
ORDER BY a.created_at DESC
LIMIT 1;"
  )"
  export FACE_ARTIFACT_ID
fi

if [[ -z "${AUDIO_ARTIFACT_ID:-}" && -z "${AUDIO_URL_INPUT:-}" ]]; then
  AUDIO_ARTIFACT_ID="$(
    docker_db_query_single "SELECT a.id::text
FROM public.artifacts a
JOIN public.studio_jobs j ON j.id = a.job_id
WHERE j.user_id = '$USER_ID'
  AND j.studio_type = 'audio'
  AND j.status = 'succeeded'
ORDER BY a.created_at DESC
LIMIT 1;"
  )"
  export AUDIO_ARTIFACT_ID
fi

echo "FACE_ARTIFACT_ID=${FACE_ARTIFACT_ID:-}"
echo "AUDIO_ARTIFACT_ID=${AUDIO_ARTIFACT_ID:-}"

python3 - "$PREVIEW_REQ" <<'PY'
import json, os, sys

out = {
    "voice_mode": "audio",
    "consent": {
        "external_provider_ok": True,
    },
}

face_artifact_id = os.environ.get("FACE_ARTIFACT_ID", "").strip()
audio_artifact_id = os.environ.get("AUDIO_ARTIFACT_ID", "").strip()
face_url = os.environ.get("FACE_URL_INPUT", "").strip()
audio_url = os.environ.get("AUDIO_URL_INPUT", "").strip()

if face_artifact_id:
    out["face_artifact_id"] = face_artifact_id
elif face_url:
    out["face_image_url"] = face_url

if audio_artifact_id:
    out["voice_audio"] = {"audio_artifact_id": audio_artifact_id}
elif audio_url:
    out["voice_audio"] = {"audio_url": audio_url}

json.dump(out, open(sys.argv[1], "w"), indent=2)
PY

if python3 - "$PREVIEW_REQ" <<'PY'
import json, sys
j = json.load(open(sys.argv[1]))
voice_audio = j.get("voice_audio") or {}
consent = j.get("consent") or {}
ok = (
    consent.get("external_provider_ok") is True
    and bool(j.get("face_artifact_id") or j.get("face_image_url"))
    and bool(voice_audio.get("audio_artifact_id") or voice_audio.get("audio_url"))
)
raise SystemExit(0 if ok else 1)
PY
then
  :
else
  echo "ERROR: missing Fusion consent or input references" >&2
  echo "Need consent.external_provider_ok=true plus one of: face_artifact_id/face_image_url and voice_audio.audio_artifact_id/audio_url" >&2
  exit 1
fi

echo
echo "==> Preview request"
pretty "$PREVIEW_REQ"

PREVIEW_HEADERS="$OUT_DIR/preview_headers.txt"
post_json_capture "$FUSION_URL/jobs/pricing/preview" "$PREVIEW_REQ" "$PREVIEW_RESP" "$PREVIEW_HEADERS" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID"

PREVIEW_STATUS="$(http_status_from_headers "$PREVIEW_HEADERS")"
echo "Preview HTTP status: $PREVIEW_STATUS"

if [[ "$PREVIEW_STATUS" != "200" ]]; then
  echo "ERROR: fusion preview failed" >&2
  cat "$PREVIEW_RESP" >&2 || true
  exit 1
fi

echo
echo "==> Preview response"
pretty "$PREVIEW_RESP"

QUOTE_ID="$(python3 - <<'PY' "$PREVIEW_RESP"
import json, sys
j=json.load(open(sys.argv[1]))
print((j.get("pricing") or {}).get("quote_id") or j.get("quote_id") or "")
PY
)"
PREVIEW_FINGERPRINT="$(python3 - <<'PY' "$PREVIEW_RESP"
import json, sys
j=json.load(open(sys.argv[1]))
print((j.get("pricing") or {}).get("preview_fingerprint") or j.get("preview_fingerprint") or "")
PY
)"

echo "QUOTE_ID=$QUOTE_ID"
echo "PREVIEW_FINGERPRINT=$PREVIEW_FINGERPRINT"

python3 - "$PREVIEW_REQ" "$QUOTE_ID" "$PREVIEW_FINGERPRINT" > "$GENERATE_REQ" <<'PY'
import json, sys
base=json.load(open(sys.argv[1]))
base["pricing_confirmation"] = {
    "quote_id": sys.argv[2],
    "preview_fingerprint": sys.argv[3],
}
print(json.dumps(base, ensure_ascii=False, indent=2))
PY

echo
echo "==> Generate request"
pretty "$GENERATE_REQ"

GENERATE_HEADERS="$OUT_DIR/generate_headers.txt"
post_json_capture "$FUSION_URL/jobs" "$GENERATE_REQ" "$GENERATE_RESP" "$GENERATE_HEADERS" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-User-Id: $USER_ID"

GENERATE_STATUS="$(http_status_from_headers "$GENERATE_HEADERS")"
echo "Generate HTTP status: $GENERATE_STATUS"

echo
echo "==> Generate response"
pretty "$GENERATE_RESP"

if [[ "$GENERATE_STATUS" != "200" && "$GENERATE_STATUS" != "201" ]]; then
  echo "ERROR: fusion create failed" >&2
  exit 1
fi

JOB_ID="$(python3 - <<'PY' "$GENERATE_RESP"
import json, sys
j=json.load(open(sys.argv[1]))
print(j.get("job_id") or j.get("id") or "")
PY
)"
if [[ -z "$JOB_ID" ]]; then
  echo "ERROR: fusion create response missing job_id" >&2
  exit 1
fi
echo "FUSION_JOB_ID=$JOB_ID"

FINAL_STATUS=""
FINAL_PRICING_STATE=""
STATUS_HEADERS="$OUT_DIR/status_headers.txt"

echo
echo "==> Polling status"
for ((i=1; i<=MAX_POLLS; i++)); do
  get_json_capture "$FUSION_URL/jobs/$JOB_ID" "$STATUS_JSON" "$STATUS_HEADERS" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-User-Id: $USER_ID"

  STATUS_HTTP="$(http_status_from_headers "$STATUS_HEADERS")"
  if [[ "$STATUS_HTTP" != "200" ]]; then
    echo "ERROR: fusion status read failed with HTTP $STATUS_HTTP" >&2
    cat "$STATUS_JSON" >&2 || true
    exit 1
  fi

  STATUS="$(python3 - <<'PY' "$STATUS_JSON"
import json, sys
j=json.load(open(sys.argv[1]))
print(j.get("status") or j.get("stage") or "")
PY
)"
  PR_STATE="$(python3 - <<'PY' "$STATUS_JSON"
import json, sys
j=json.load(open(sys.argv[1]))
print((j.get("pricing") or {}).get("state") or "")
PY
)"

  echo "poll=$i status=${STATUS:-<empty>} pricing_state=${PR_STATE:-<empty>}"

  if [[ "$STATUS" == "succeeded" || "$STATUS" == "failed" || "$STATUS" == "canceled" || "$STATUS" == "cancelled" ]]; then
    FINAL_STATUS="$STATUS"; FINAL_PRICING_STATE="$PR_STATE"; break
  fi
  sleep "$POLL_SECS"
done

if [[ -z "$FINAL_STATUS" ]]; then
  echo "ERROR: fusion job did not reach terminal state" >&2
  pretty "$STATUS_JSON"
  exit 1
fi

echo
echo "==> Final status response"
pretty "$STATUS_JSON"

python3 - "$STATUS_JSON" "$SUMMARY_JSON" "$JOB_ID" "$QUOTE_ID" "$PREVIEW_FINGERPRINT" <<'PY'
import json, sys
status_path, summary_path, job_id, quote_id, preview_fp = sys.argv[1:]
status = json.load(open(status_path))
pricing = status.get("pricing") or {}
summary = status.get("pricing_summary") or {}
out = {
    "job_id": job_id,
    "quote_id": quote_id,
    "preview_fingerprint": preview_fp,
    "status": status.get("status") or status.get("stage"),
    "pricing_state": pricing.get("state"),
    "billing_mode": pricing.get("billing_mode"),
    "settlement_mode": pricing.get("settlement_mode"),
    "tier_code": pricing.get("tier_code"),
    "tier_source": pricing.get("tier_source"),
    "entitlement_source": pricing.get("entitlement_source"),
    "entitlement_reason": pricing.get("entitlement_reason"),
    "sku_code": pricing.get("sku_code"),
    "amount": pricing.get("amount"),
    "currency": pricing.get("currency"),
    "actual_units": pricing.get("actual_units"),
    "billed_units": pricing.get("billed_units"),
    "reservation_id": pricing.get("reservation_id"),
    "pricing_summary": summary,
    "full_status": status,
}
json.dump(out, open(summary_path, "w"), indent=2, ensure_ascii=False)
print(json.dumps(out, indent=2, ensure_ascii=False))
PY

echo
echo "SUMMARY_JSON=$SUMMARY_JSON"

if [[ "$FINAL_STATUS" == "succeeded" && "$FINAL_PRICING_STATE" != "committed" ]]; then
  echo "ERROR: fusion job succeeded but pricing_state=$FINAL_PRICING_STATE" >&2
  exit 2
fi
if [[ "$FINAL_STATUS" == "failed" || "$FINAL_STATUS" == "canceled" || "$FINAL_STATUS" == "cancelled" ]]; then
  if [[ "$FINAL_PRICING_STATE" != "released" ]]; then
    echo "ERROR: fusion job terminal failure/cancel but pricing_state=$FINAL_PRICING_STATE" >&2
    exit 3
  fi
fi

if command -v docker >/dev/null 2>&1; then
  echo
  echo "==> Latest fusion pricing reservations"
  docker exec -i desifaces-db bash -lc "psql -U \"\$POSTGRES_USER\" -d \"\${POSTGRES_DB:-postgres}\" -c \"
SELECT
  id,
  user_id,
  status,
  service_name,
  service_action,
  sku_code,
  billing_account_id,
  settlement_mode,
  estimated_money,
  currency,
  created_at
FROM public.pricing_credit_reservations
WHERE service_name = 'svc-fusion'
ORDER BY created_at DESC
LIMIT 5;
\"" || true
fi

echo
echo "Fusion pricing E2E passed."
