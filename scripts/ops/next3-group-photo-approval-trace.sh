#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/next3-group-photo-approval-trace-${STAMP}.txt"
TOKEN="shared_scene_media_not_owned_active_face_image"

{
  echo "============================================================"
  echo " NEXT3 — GROUP PHOTO APPROVAL FAILURE TRACE"
  echo "============================================================"
  echo "timestamp=$STAMP"

  echo
  echo "===== CONTAINER STATE ====="
  for C in df-svc-director df-svc-face df-svc-fusion-extension df-web-dev; do
    if docker inspect "$C" >/dev/null 2>&1; then
      docker inspect "$C" --format 'name={{.Name}} image={{.Config.Image}} image_id={{.Image}} status={{.State.Status}} restarts={{.RestartCount}}'
    else
      echo "$C MISSING"
    fi
  done

  echo
  echo "===== ERROR TOKEN IN LIVE SOURCE ====="
  for C in df-svc-director df-svc-face df-svc-fusion-extension; do
    echo "--- $C ---"
    timeout 10s docker exec "$C" sh -lc       "grep -Rsn --include='*.py' '$TOKEN' /app 2>/dev/null | head -n 20" || true
  done

  echo
  echo "===== DIRECTOR SHARED-SCENE ROUTES ====="
  timeout 10s docker exec df-svc-director python -c     'from app.main import app; [print(",".join(sorted(r.methods or [])), r.path) for r in app.routes if "shared-scene" in getattr(r,"path","") or "group" in getattr(r,"path","")]'     || true

  echo
  echo "===== DIRECTOR RECENT FAILURE ====="
  docker logs --since 15m --tail 1500 --timestamps df-svc-director 2>&1     | grep -Ei -C 12       'shared_scene_media_not_owned_active_face_image|shared-scene|group.photo|media|approval|500 Internal|409 |422 |Traceback|ERROR:'     | tail -n 220 || true

  echo
  echo "===== FACE RECENT FAILURE ====="
  docker logs --since 15m --tail 1000 --timestamps df-svc-face 2>&1     | grep -Ei -C 10       'shared_scene_media_not_owned_active_face_image|shared-scene|group.photo|media|active.face|owner|500 Internal|409 |422 |Traceback|ERROR:'     | tail -n 160 || true

  echo
  echo "===== WEB RECENT FAILURE ====="
  docker logs --since 15m --tail 1000 --timestamps df-web-dev 2>&1     | grep -Ei -C 8       'shared_scene_media_not_owned_active_face_image|shared-scene|group.photo|approve|HTTP 4|HTTP 5|Internal Server|fetch failed'     | tail -n 140 || true

  echo
  echo "============================================================"
  echo " NEXT3_GROUP_PHOTO_APPROVAL_TRACE=PASS"
  echo "============================================================"
} > "$OUT" 2>&1

echo "TRACE_FILE=$OUT"
echo "----- LAST 120 LINES -----"
tail -n 120 "$OUT"
