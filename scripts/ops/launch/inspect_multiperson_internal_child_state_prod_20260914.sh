#!/usr/bin/env bash
set -Eeuo pipefail

IDS=(
  "6470d989-aa23-5c07-883a-8bb3c990166f"
  "964f8fb3-4a2c-4df6-968a-34b283d9ccd0"
)

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"

DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
FUSION="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-fusion$|^df-svc-fusion$|svc-fusion$' | head -1 || true)"
WORKER="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-fusion-worker$|^df-svc-fusion-worker$|svc-fusion-worker$' | head -1 || true)"

[[ -n "$DIRECTOR" ]] || fail "Director container missing"
[[ -n "$FUSION" ]] || fail "Fusion API container missing"
[[ -n "$WORKER" ]] || fail "Fusion worker container missing"

echo "============================================================"
echo " desifaces — READ-ONLY MULTI-PERSON INTERNAL CHILD STATE"
echo " mutation=NONE"
echo "============================================================"

echo "DIRECTOR=$DIRECTOR"
echo "FUSION_API=$FUSION"
echo "FUSION_WORKER=$WORKER"
echo "FUSION_API_STATE=$(docker inspect -f '{{.State.Status}}' "$FUSION")"
echo "FUSION_WORKER_STATE=$(docker inspect -f '{{.State.Status}}' "$WORKER")"
echo "FUSION_WORKER_RESTART_COUNT=$(docker inspect -f '{{.RestartCount}}' "$WORKER")"
echo

for ID in "${IDS[@]}"; do
  echo "===== JOB PROBE $ID ====="
  docker exec "$DIRECTOR" python -c '
import sys,urllib.request,urllib.error
from app.config import settings
jid=sys.argv[1]
base=str(settings.DF_FUSION_BASE_URL).rstrip("/")
print("FUSION_BASE="+base)
for suffix in ("/status-light","/status"):
    url=f"{base}/jobs/{jid}{suffix}"
    try:
        with urllib.request.urlopen(url,timeout=8) as r:
            body=r.read().decode("utf-8","replace")
            print(f"URL={url}")
            print(f"HTTP={r.status}")
            print("BODY="+body[:4000])
    except urllib.error.HTTPError as e:
        body=e.read().decode("utf-8","replace")
        print(f"URL={url}")
        print(f"HTTP={e.code}")
        print("BODY="+body[:4000])
    except Exception as e:
        print(f"URL={url}")
        print("ERROR="+repr(e))
' "$ID"
  echo
done

echo "===== FUSION WORKER RECENT LOG EVIDENCE ====="
docker logs --since 2h --tail 1200 "$WORKER" 2>&1 \
  | grep -E '6470d989|964f8fb3|claim|claimed|queued|running|provider|error|exception|failed|stale|recover' \
  | tail -n 220 || true

echo "============================================================"
echo "READ_ONLY_JOB_STATE_INSPECTION=PASS"
echo "MUTATION=NONE"
echo "============================================================"
