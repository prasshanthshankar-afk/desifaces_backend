#!/usr/bin/env bash
set -euo pipefail

export CORE_URL="${CORE_URL:-http://localhost:8000}"
export AUDIO_URL="${AUDIO_URL:-http://localhost:8004}"
export DF_EMAIL="${DF_EMAIL:-user2@desifaces.ai}"
export DF_PASSWORD="${DF_PASSWORD:-password2}"
export MAX_POLLS="${MAX_POLLS:-120}"
export POLL_SECS="${POLL_SECS:-3}"

OUT_DIR="${OUT_DIR:-/tmp/df_e2e_audio_with_pricing_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT_DIR"

AUTH_JSON="$OUT_DIR/auth.json"
PREVIEW_REQ="$OUT_DIR/audio_preview_req.json"
PREVIEW_RESP="$OUT_DIR/audio_preview_resp.json"
GENERATE_REQ="$OUT_DIR/audio_generate_req.json"
GENERATE_RESP="$OUT_DIR/audio_generate_resp.json"
STATUS_JSON="$OUT_DIR/audio_status.json"
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

post_json() {
  local url="$1"; local payload="$2"; local output="$3"; shift 3
  curl -sS -X POST "$url" -H "Content-Type: application/json" "$@" --data @"$payload" > "$output"
}

get_json() {
  local url="$1"; local output="$2"; shift 2
  curl -sS "$url" "$@" > "$output"
}

echo "OUT_DIR=$OUT_DIR"
echo "CORE_URL=$CORE_URL"
echo "AUDIO_URL=$AUDIO_URL"
echo "DF_EMAIL=$DF_EMAIL"

cat > "$OUT_DIR/login_request.json" <<JSON
{"email":"$DF_EMAIL","password":"$DF_PASSWORD"}
JSON

echo
echo "==> Login"
post_json "$CORE_URL/api/auth/login" "$OUT_DIR/login_request.json" "$AUTH_JSON"
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

export AUDIO_TEST_TEXT="${AUDIO_TEST_TEXT:-Hello from DesiFaces. This is a pricing validation test for Audio Studio.}"
RUN_TAG="${RUN_TAG:-$(date +%s)}"

python3 - "$PREVIEW_REQ" "$AUDIO_TEST_TEXT" "$RUN_TAG" <<'PY'
import json, sys
out_path, text, run_tag = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {
    "text": f"{text} [run:{run_tag}]",
    "target_locale": "en-US",
    "output_format": "mp3",
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
PY

echo
echo "==> Preview request"
pretty "$PREVIEW_REQ"

AUDIO_PREVIEW_PATHS=(
  "/api/audio/tts/pricing/preview"
  "/api/audio/pricing/preview"
  "/api/audio/tts/preview"
)

PREVIEW_OK=0
for p in "${AUDIO_PREVIEW_PATHS[@]}"; do
  echo "Trying preview path: $p"
  set +e
  post_json "$AUDIO_URL$p" "$PREVIEW_REQ" "$PREVIEW_RESP"     -H "Authorization: Bearer $TOKEN"     -H "X-User-Id: $USER_ID"
  rc=$?
  set -e
  if [[ $rc -eq 0 ]] && python3 - "$PREVIEW_RESP" <<'PY'
import json, sys
try:
    j = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
ok = bool(j.get("quote_id") or (j.get("pricing") or {}).get("quote_id") or j.get("pricing") or j.get("pricing_summary"))
raise SystemExit(0 if ok else 1)
PY
  then
    PREVIEW_PATH="$p"; PREVIEW_OK=1; break
  fi
done

if [[ $PREVIEW_OK -ne 1 ]]; then
  echo "ERROR: audio preview failed on all candidate paths" >&2
  cat "$PREVIEW_RESP" >&2 || true
  exit 1
fi

echo
echo "==> Preview response ($PREVIEW_PATH)"
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

AUDIO_GENERATE_PATHS=(
  "/api/audio/tts"
  "/api/audio/tts/generate"
  "/api/audio/generate"
)

GENERATE_OK=0
for p in "${AUDIO_GENERATE_PATHS[@]}"; do
  echo "Trying generate path: $p"
  set +e
  post_json "$AUDIO_URL$p" "$GENERATE_REQ" "$GENERATE_RESP"     -H "Authorization: Bearer $TOKEN"     -H "X-User-Id: $USER_ID"
  rc=$?
  set -e
  if [[ $rc -eq 0 ]] && python3 - "$GENERATE_RESP" <<'PY'
import json, sys
try:
    j=json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
ok = bool(j.get("job_id") or j.get("id"))
raise SystemExit(0 if ok else 1)
PY
  then
    GENERATE_PATH="$p"; GENERATE_OK=1; break
  fi
done

if [[ $GENERATE_OK -ne 1 ]]; then
  echo "ERROR: audio generate failed on all candidate paths" >&2
  cat "$GENERATE_RESP" >&2 || true
  exit 1
fi

echo
echo "==> Generate response ($GENERATE_PATH)"
pretty "$GENERATE_RESP"

JOB_ID="$(python3 - <<'PY' "$GENERATE_RESP"
import json, sys
j=json.load(open(sys.argv[1]))
print(j.get("job_id") or j.get("id") or "")
PY
)"
echo "AUDIO_JOB_ID=$JOB_ID"

AUDIO_STATUS_PATHS=(
  "/api/audio/jobs/$JOB_ID/status"
  "/api/audio/tts/jobs/$JOB_ID/status"
  "/api/audio/jobs/$JOB_ID"
)

FINAL_STATUS=""
FINAL_PRICING_STATE=""
STATUS_PATH=""

echo
echo "==> Polling status"
for ((i=1; i<=MAX_POLLS; i++)); do
  STATUS_OK=0
  for p in "${AUDIO_STATUS_PATHS[@]}"; do
    set +e
    get_json "$AUDIO_URL$p" "$STATUS_JSON"       -H "Authorization: Bearer $TOKEN"       -H "X-User-Id: $USER_ID"
    rc=$?
    set -e
    if [[ $rc -eq 0 ]] && python3 - "$STATUS_JSON" <<'PY'
import json, sys
try:
    json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
raise SystemExit(0)
PY
    then
      STATUS_PATH="$p"; STATUS_OK=1; break
    fi
  done

  if [[ $STATUS_OK -ne 1 ]]; then
    echo "ERROR: could not read audio status from any candidate path" >&2
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

  echo "poll=$i status=${STATUS:-<empty>} pricing_state=${PR_STATE:-<empty>} path=$STATUS_PATH"

  if [[ "$STATUS" == "succeeded" || "$STATUS" == "failed" || "$STATUS" == "cancelled" ]]; then
    FINAL_STATUS="$STATUS"; FINAL_PRICING_STATE="$PR_STATE"; break
  fi
  sleep "$POLL_SECS"
done

if [[ -z "$FINAL_STATUS" ]]; then
  echo "ERROR: audio job did not reach terminal state" >&2
  pretty "$STATUS_JSON"
  exit 1
fi

echo
echo "==> Final status response ($STATUS_PATH)"
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
  echo "ERROR: audio job succeeded but pricing_state=$FINAL_PRICING_STATE" >&2
  exit 2
fi
if [[ "$FINAL_STATUS" == "failed" || "$FINAL_STATUS" == "cancelled" ]]; then
  if [[ "$FINAL_PRICING_STATE" != "released" ]]; then
    echo "ERROR: audio job terminal failure/cancel but pricing_state=$FINAL_PRICING_STATE" >&2
    exit 3
  fi
fi

echo
echo "Audio pricing E2E passed."
