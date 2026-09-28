#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

DIRECTOR="df-svc-director"
WEB="df-web-dev"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/next3-reload-trace-${STAMP}.txt"

{
  echo "============================================================"
  echo " NEXT3 — SAFE RELOAD / RECENT STORIES TRACE"
  echo "============================================================"
  echo "timestamp=$STAMP"

  echo
  echo "===== CONTAINER STATE ====="
  docker inspect "$DIRECTOR" --format 'director image={{.Config.Image}} image_id={{.Image}} status={{.State.Status}} started={{.State.StartedAt}} restarts={{.RestartCount}}'
  docker inspect "$WEB" --format 'web image={{.Config.Image}} image_id={{.Image}} status={{.State.Status}} started={{.State.StartedAt}} restarts={{.RestartCount}}'

  echo
  echo "===== HEALTH ====="
  timeout 8s docker exec "$DIRECTOR" sh -lc 'curl -fsS -o /dev/null -w "director=%{http_code}\n" http://127.0.0.1:${PORT:-8011}/api/health' || true
  timeout 8s curl -fsS -o /dev/null -w 'web=%{http_code}\n' http://127.0.0.1:13000/auth/login || true

  echo
  echo "===== DIRECTOR RECENT-STORIES / ERROR TRACE ====="
  docker logs --since 15m --tail 1200 --timestamps "$DIRECTOR" 2>&1     | grep -Ei -C 10 'stories/recent|Internal Server Error|Traceback|ERROR:|Exception|asyncpg|500 '     | tail -n 180 || true

  echo
  echo "===== WEB GATEWAY TRACE ====="
  docker logs --since 15m --tail 800 --timestamps "$WEB" 2>&1     | grep -Ei -C 8 'stories/recent|Internal Server Error|fetch failed|HTTP 500|500 '     | tail -n 120 || true

  echo
  echo "============================================================"
  echo " NEXT3_SAFE_RELOAD_TRACE=PASS"
  echo "============================================================"
} > "$OUT" 2>&1

echo "TRACE_FILE=$OUT"
echo "----- LAST 80 LINES -----"
tail -n 80 "$OUT"
