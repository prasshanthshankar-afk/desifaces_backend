#!/usr/bin/env bash
set -Eeuo pipefail

EXT_API="df-svc-fusion-extension"
fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"

BACKUP="$(ls -1dt /tmp/df-fusion-parent-pricing-compat-* 2>/dev/null | head -1 || true)"
[[ -n "$BACKUP" && -d "$BACKUP" ]] || fail "pricing-compat backup not found"
[[ -f "$BACKUP/app/main.py" ]] || fail "backup main.py missing"

echo "============================================================"
echo " desifaces — FUSION EXTENSION EMERGENCY RESTORE"
echo " source=PRE-HOTFIX_RUNTIME_BACKUP"
echo " scope=FUSION_EXTENSION_API_ONLY"
echo "============================================================"

echo "backup=$BACKUP"

docker cp "$BACKUP/app/main.py" "$EXT_API:/app/app/main.py"
if [[ -f "$BACKUP/pricing_compat.absent" ]]; then
  docker exec "$EXT_API" rm -f /app/app/api/routes/pricing_compat.py || true
elif [[ -f "$BACKUP/app/api/routes/pricing_compat.py" ]]; then
  docker cp "$BACKUP/app/api/routes/pricing_compat.py" "$EXT_API:/app/app/api/routes/pricing_compat.py"
fi

echo "PRE_HOTFIX_RUNTIME_RESTORED=PASS"

docker restart "$EXT_API" >/dev/null

STATE=""; HEALTH=""
for _ in $(seq 1 30); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT_API" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT_API" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 3
done

[[ "$STATE" == "running" ]] || fail "Fusion Extension API still not running after restore"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension API unhealthy after restore: $HEALTH"

docker exec -i "$EXT_API" python - <<'PY'
import urllib.request, json
with urllib.request.urlopen('http://127.0.0.1:8006/api/health', timeout=8) as r:
    body=json.loads(r.read().decode())
    assert r.status == 200
    assert str(body.get('status') or '').lower() == 'ok'
print('FUSION_EXTENSION_HTTP_HEALTH=PASS')
PY

echo "FUSION_EXTENSION_STATE=$STATE"
echo "FUSION_EXTENSION_HEALTH=$HEALTH"
echo "FACE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "CORE_FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "PRICING_TOUCH=NONE"
echo "DB_TOUCH=NONE"
echo "WEB_TOUCH=NONE"
echo "PRODUCTION_RECOVERY=PASS"
echo "CHANGE_FREEZE=RECOMMENDED"
