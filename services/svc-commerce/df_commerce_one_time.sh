#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://localhost:8008}"
IDEM="${IDEM:-demo-001}"
RUN_DIR="${RUN_DIR:-/tmp/df_commerce_one_time_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$RUN_DIR"

HDR="$RUN_DIR/hdr.txt"
OUT="$RUN_DIR/out.bin"
REQ="$RUN_DIR/req.json"

# ---- hard safety limits to avoid terminal crash ----
MAX_PREVIEW_BYTES="${MAX_PREVIEW_BYTES:-300}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 2; }; }
need curl
need python3

small_preview() {
  echo "---- HTTP headers (last line) ----"
  tail -n 1 "$HDR" 2>/dev/null | tr -d '\r' || true
  echo "---- Body preview (first ${MAX_PREVIEW_BYTES} bytes) ----"
  head -c "$MAX_PREVIEW_BYTES" "$OUT" 2>/dev/null || true
  echo
  echo "Saved: $OUT"
}

# curl wrapper that NEVER prints body to terminal
curl_call() {
  local method="$1"; shift
  local path="$1"; shift
  local data_file="${1:-}"

  : >"$HDR"
  : >"$OUT"

  local tries=3
  local attempt=1
  local code="000"

  while [ "$attempt" -le "$tries" ]; do
    if [ -n "$data_file" ]; then
      code="$(
        curl -q -sS \
          --connect-timeout 8 --max-time 60 \
          --retry 0 \
          -D "$HDR" -o "$OUT" -w "%{http_code}" \
          -X "$method" "$BASE$path" \
          -H "Authorization: Bearer $TOKEN" \
          -H "Content-Type: application/json" \
          --data-binary "@$data_file" || true
      )"
    else
      code="$(
        curl -q -sS \
          --connect-timeout 8 --max-time 60 \
          --retry 0 \
          -D "$HDR" -o "$OUT" -w "%{http_code}" \
          -X "$method" "$BASE$path" \
          -H "Authorization: Bearer $TOKEN" || true
      )"
    fi

    # success
    if [ "$code" = "200" ] || [ "$code" = "201" ]; then
      echo "$code"
      return 0
    fi

    # retry on transient 5xx / 000
    if [ "$code" = "000" ] || [[ "$code" =~ ^5 ]]; then
      sleep 0.5
      attempt=$((attempt + 1))
      continue
    fi

    # non-retryable (4xx etc.)
    echo "$code"
    return 0
  done

  echo "$code"
}

# safe json key extraction without jq; prints empty if missing / invalid json
json_key() {
  local key="$1"
  python3 - "$OUT" "$key" <<'PY'
import json, sys
p, k = sys.argv[1], sys.argv[2]
try:
    with open(p, "rb") as f:
        b = f.read()
    s = b.decode("utf-8", "ignore")
    j = json.loads(s)
except Exception:
    print("")
    sys.exit(0)
if isinstance(j, dict) and k in j and j[k] is not None:
    v = j[k]
    print(v if isinstance(v, str) else str(v))
else:
    print("")
PY
}

echo "RUN_DIR=$RUN_DIR"

# quick sanity (doesn't print json)
CODE="$(curl_call GET /api/health)"
if [ "$CODE" != "200" ]; then
  echo "ERROR: svc-commerce health failed code=$CODE"
  small_preview
  exit 1
fi
echo "health=OK"

# ---- 1) QUOTE ----
cat >"$REQ" <<'JSON'
{"mode":"platform_models","product_type":"apparel","product_ids":[],"look_set_ids":[],"outputs":{"num_images":4,"num_videos":1},"resolution":"hd","people":["solo_female"],"views":{"half_body":true,"full_body":false},"channels":["instagram"],"marketplaces":[]}
JSON

CODE="$(curl_call POST /api/commerce/quote "$REQ")"
if [ "$CODE" != "200" ]; then
  echo "ERROR: quote failed code=$CODE"
  small_preview
  exit 1
fi

QID="$(json_key quote_id)"
if [ -z "$QID" ]; then
  echo "ERROR: quote_id missing (quote response not valid JSON or missing key)"
  small_preview
  exit 1
fi
echo "quote_id=$QID"

# ---- 2) CONFIRM ----
cat >"$REQ" <<JSON
{"quote_id":"$QID","idempotency_key":"$IDEM"}
JSON

CODE="$(curl_call POST /api/commerce/confirm "$REQ")"
if [ "$CODE" != "200" ]; then
  echo "ERROR: confirm failed code=$CODE"
  small_preview
  exit 1
fi

CAMPAIGN_ID="$(json_key campaign_id)"
SID="$(json_key studio_job_id)"

echo "campaign_id=${CAMPAIGN_ID:-}"
echo "studio_job_id=${SID:-}"

if [ -z "$SID" ]; then
  echo "ERROR: studio_job_id missing in confirm response"
  small_preview
  exit 1
fi

# ---- 3) STATUS ----
CODE="$(curl_call GET "/api/commerce/jobs/$SID/status")"
if [ "$CODE" != "200" ]; then
  echo "ERROR: status failed code=$CODE"
  small_preview
  exit 1
fi

JOB_STATUS="$(json_key status)"
echo "job_status=${JOB_STATUS:-unknown}"

echo "DONE. All artifacts saved under: $RUN_DIR"
echo "Files:"
echo "  $RUN_DIR/hdr.txt"
echo "  $RUN_DIR/out.bin"
echo "  $RUN_DIR/req.json"