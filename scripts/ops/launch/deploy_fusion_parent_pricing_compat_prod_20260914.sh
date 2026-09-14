#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_SHA="3a4cbe6c22815320db088facea4850eaa83f978c"
EXT_API="df-svc-fusion-extension"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/tmp/df-fusion-parent-pricing-compat-${STAMP}"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$EXT_API")" == "running" ]] || fail "$EXT_API not running"

mkdir -p "$BACKUP/app/api/routes"
docker cp "$EXT_API:/app/app/main.py" "$BACKUP/app/main.py"
if docker exec "$EXT_API" test -f /app/app/api/routes/pricing_compat.py; then
  docker cp "$EXT_API:/app/app/api/routes/pricing_compat.py" "$BACKUP/app/api/routes/pricing_compat.py"
else
  touch "$BACKUP/pricing_compat.absent"
fi

rollback(){
  rc=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker cp "$BACKUP/app/main.py" "$EXT_API:/app/app/main.py" || true
  if [[ -f "$BACKUP/pricing_compat.absent" ]]; then
    docker exec "$EXT_API" rm -f /app/app/api/routes/pricing_compat.py || true
  else
    docker cp "$BACKUP/app/api/routes/pricing_compat.py" "$EXT_API:/app/app/api/routes/pricing_compat.py" || true
  fi
  docker restart "$EXT_API" >/dev/null 2>&1 || true
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap rollback ERR

printf '%s\n' "============================================================"
printf '%s\n' " desifaces — MULTI-PERSON PARENT PRICING ROUTE HOTFIX"
printf ' source_sha=%s\n' "$SOURCE_SHA"
printf '%s\n' " scope=FUSION_EXTENSION_API_ONLY"
printf '%s\n' "============================================================"

printf '%s\n' "===== 1. FETCH PINNED SOURCE ====="
TMP="$(mktemp -d)"
mkdir -p "$TMP/app/api/routes"
for rel in app/main.py app/api/routes/pricing_compat.py; do
  curl -fsSL --connect-timeout 5 --max-time 20 \
    "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/svc-fusion-extension/app/${rel}" \
    -o "$TMP/$rel"
done
python3 -m py_compile "$TMP/app/main.py" "$TMP/app/api/routes/pricing_compat.py"
grep -Fq '/api/longform/jobs/pricing/preview' "$TMP/app/api/routes/pricing_compat.py" || fail "legacy alias missing"
grep -Fq 'return await preview_longform' "$TMP/app/api/routes/pricing_compat.py" || fail "canonical delegation missing"
echo "PINNED_SOURCE=PASS"

printf '%s\n' "===== 2. PATCH FUSION EXTENSION API ONLY ====="
docker cp "$TMP/app/main.py" "$EXT_API:/app/app/main.py"
docker cp "$TMP/app/api/routes/pricing_compat.py" "$EXT_API:/app/app/api/routes/pricing_compat.py"
echo "FUSION_EXTENSION_RUNTIME_PATCHED=PASS"

printf '%s\n' "===== 3. RESTART API ONLY ====="
docker restart "$EXT_API" >/dev/null

STATE=""; HEALTH=""
for _ in $(seq 1 36); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT_API" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT_API" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 5
done
[[ "$STATE" == "running" ]] || fail "Fusion Extension API not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension API unhealthy: $HEALTH"
echo "FUSION_EXTENSION_HEALTH=PASS"

printf '%s\n' "===== 4. LIVE ROUTE CERTIFICATION ====="
docker exec -i "$EXT_API" python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
required={
    '/api/longform/pricing/preview',
    '/api/longform/jobs/pricing/preview',
    '/api/fusion/jobs/pricing/preview',
    '/jobs/pricing/preview',
}
missing=sorted(required-paths)
assert not missing, missing
print('CANONICAL_PARENT_PRICING_ROUTE=PASS')
print('LEGACY_PARENT_PRICING_ALIAS=PASS')
print('DIRECTOR_PARENT_PRICING_COMPAT=PASS')
PY

trap - ERR
rm -rf "$TMP"

echo "fusion_extension_state=$STATE"
echo "fusion_extension_health=$HEALTH"
echo "MULTI_PERSON_PARENT_PRICING_404_FIX=DEPLOYED"
echo "FACE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "CORE_FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "PRICING_LOGIC_TOUCH=NONE"
echo "DB_SCHEMA_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
echo "MULTI_PERSON_LAUNCH_HOTFIX=PASS"
echo "============================================================"
