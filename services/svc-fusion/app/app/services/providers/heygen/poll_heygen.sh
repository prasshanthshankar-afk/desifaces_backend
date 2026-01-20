set -u
# NOTE: intentionally NOT using `set -e` so the terminal never dies.

: "${DF_HEYGEN_API_KEY:?DF_HEYGEN_API_KEY is required}"

VID="${VID:-df7f951897ed4ae588e956c1c86d91af}"
LIMIT="${LIMIT:-100}"          # HeyGen limit must be <= 100
SLEEP_SEC="${SLEEP_SEC:-5}"
MAX_MINUTES="${MAX_MINUTES:-20}"

deadline=$(( $(date +%s) + MAX_MINUTES*60 ))

echo "Polling HeyGen video until completed (max ${MAX_MINUTES} min): ${VID}"

while true; do
  now=$(date +%s)
  if [ "$now" -ge "$deadline" ]; then
    echo "TIMEOUT after ${MAX_MINUTES} minutes. Last VID=${VID}"
    break
  fi

  tmp_headers="$(mktemp -t heygen_hdr.XXXXXX)"
  tmp_body="$(mktemp -t heygen_body.XXXXXX)"

  # Fetch (capture headers + body). Do NOT let curl kill the script.
  curl -sS --http1.1 -D "$tmp_headers" -o "$tmp_body" \
    "https://api.heygen.com/v1/video.list?limit=${LIMIT}" \
    -H "X-Api-Key: ${DF_HEYGEN_API_KEY}" \
    -H "Accept: application/json" >/dev/null 2>&1

  http_code="$(awk 'NR==1{print $2}' "$tmp_headers" 2>/dev/null || echo "")"

  # If we didn't even get an HTTP code, treat as transient.
  if [ -z "${http_code}" ]; then
    echo "NO_HTTP_CODE (transient). Retrying..."
    rm -f "$tmp_headers" "$tmp_body"
    sleep "$SLEEP_SEC"
    continue
  fi

  # Handle common transient auth issue (env not exported in that shell)
  if [ "$http_code" = "401" ]; then
    echo "HTTP 401 Unauthorized. Check DF_HEYGEN_API_KEY is exported in *this* terminal. Retrying..."
    rm -f "$tmp_headers" "$tmp_body"
    sleep "$SLEEP_SEC"
    continue
  fi

  # Non-200: print body (but don’t exit terminal)
  if [ "$http_code" != "200" ]; then
    echo "HTTP ${http_code} (non-200). Body:"
    head -c 400 "$tmp_body"; echo
    rm -f "$tmp_headers" "$tmp_body"
    sleep "$SLEEP_SEC"
    continue
  fi

  # 200 but empty body => retry
  if [ ! -s "$tmp_body" ]; then
    echo "HTTP 200 but EMPTY_BODY (retrying)."
    rm -f "$tmp_headers" "$tmp_body"
    sleep "$SLEEP_SEC"
    continue
  fi

  # Parse JSON safely. Any parse error => transient retry.
  python3 - <<'PY' "$tmp_body" "$VID"
import json, sys, os

path=sys.argv[1]
vid=sys.argv[2]

try:
    with open(path, "rb") as f:
        raw=f.read()
    if not raw.strip():
        print("EMPTY_BODY")
        sys.exit(2)

    data=json.loads(raw.decode("utf-8", errors="replace"))
    videos=(data.get("data") or {}).get("videos") or []
    item=None
    for v in videos:
        if str(v.get("video_id"))==str(vid):
            item=v
            break

    if not item:
        print("NOT_FOUND_YET")
        sys.exit(2)

    status=str(item.get("status","processing")).lower()
    print("STATUS:", status)

    # If completed, print the full record (it may include video_url)
    if status == "completed":
        print("ITEM:", json.dumps(item, ensure_ascii=False))
        sys.exit(0)

    if status in ("failed","error"):
        print("FAILED_ITEM:", json.dumps(item, ensure_ascii=False))
        sys.exit(1)

    sys.exit(2)

except Exception as e:
    print("PARSE_ERROR:", repr(e))
    sys.exit(3)
PY

  rc=$?

  # Clean temp files every loop
  rm -f "$tmp_headers" "$tmp_body"

  if [ $rc -eq 0 ]; then
    echo "COMPLETED ✅"
    break
  elif [ $rc -eq 1 ]; then
    echo "FAILED ❌ (see FAILED_ITEM above)"
    break
  elif [ $rc -eq 3 ]; then
    echo "PARSE_ERROR (transient). Retrying..."
  fi

  sleep "$SLEEP_SEC"
done