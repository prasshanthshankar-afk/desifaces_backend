#!/usr/bin/env bash
set -Eeuo pipefail

EXT_API="df-svc-fusion-extension"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/df-multiperson-parent-pricing-complete-${STAMP}"
mkdir -p "$RUN"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"

echo "============================================================"
echo " desifaces — COMPLETE MULTI-PERSON FUSION PARENT PRICING"
echo " scope=FUSION_EXTENSION_API_ONLY"
echo " strategy=RESTORE_THEN_CANONICAL_ALIAS"
echo "============================================================"

# -----------------------------------------------------------------------------
# 1. Restore the exact pre-failed-hotfix Fusion Extension runtime first.
# -----------------------------------------------------------------------------
echo "===== 1. RESTORE LAST HEALTHY FUSION EXTENSION RUNTIME ====="
PRE="$(ls -1dt /tmp/df-fusion-parent-pricing-compat-* 2>/dev/null | head -1 || true)"
[[ -n "$PRE" && -d "$PRE" ]] || fail "pre-hotfix Fusion Extension backup missing"
[[ -f "$PRE/app/main.py" ]] || fail "pre-hotfix main.py missing"

docker cp "$PRE/app/main.py" "$EXT_API:/app/app/main.py"
if [[ -f "$PRE/pricing_compat.absent" ]]; then
  docker exec "$EXT_API" rm -f /app/app/api/routes/pricing_compat.py >/dev/null 2>&1 || true
elif [[ -f "$PRE/app/api/routes/pricing_compat.py" ]]; then
  docker cp "$PRE/app/api/routes/pricing_compat.py" "$EXT_API:/app/app/api/routes/pricing_compat.py"
fi

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
if [[ "$STATE" != "running" ]]; then
  docker logs "$EXT_API" --tail 80 >&2 || true
  fail "pre-hotfix Fusion Extension runtime did not recover"
fi
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "pre-hotfix Fusion Extension unhealthy: $HEALTH"
echo "PRE_HOTFIX_RUNTIME_RESTORED=PASS"
echo "FUSION_EXTENSION_BASELINE_HEALTH=PASS"

# -----------------------------------------------------------------------------
# 2. Snapshot ONLY the canonical longform route before the one-line alias patch.
# -----------------------------------------------------------------------------
echo "===== 2. SNAPSHOT CANONICAL LONGFORM ROUTE ====="
docker cp "$EXT_API:/app/app/api/routes/longform.py" "$RUN/longform.py.before"

rollback(){
  rc=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  if [[ -f "$RUN/longform.py.before" ]]; then
    docker cp "$RUN/longform.py.before" "$EXT_API:/app/app/api/routes/longform.py" >/dev/null 2>&1 || true
    docker restart "$EXT_API" >/dev/null 2>&1 || true
  fi
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap rollback ERR

# -----------------------------------------------------------------------------
# 3. Add exactly one backward-compatible decorator to the canonical handler.
#    No duplicate pricing implementation. No new module. No main.py changes.
# -----------------------------------------------------------------------------
echo "===== 3. ADD LEGACY URL ALIAS TO CANONICAL HANDLER ====="
docker exec -i "$EXT_API" python - <<'PY'
from pathlib import Path

p = Path('/app/app/api/routes/longform.py')
s = p.read_text()

alias = '@router.post("/jobs/pricing/preview", response_model=LongformPricingPreviewResponse, include_in_schema=False)\n'
anchor = '@router.post("/pricing/preview", response_model=LongformPricingPreviewResponse)\nasync def preview_longform('

if alias not in s:
    if anchor not in s:
        raise SystemExit('canonical preview_longform decorator anchor not found')
    s = s.replace(anchor, alias + anchor, 1)
    p.write_text(s)

s2 = p.read_text()
assert alias in s2
assert anchor in s2
print('CANONICAL_ALIAS_SOURCE_PATCH=PASS')
PY

# Syntax check is not enough: import the full app before any restart.
docker exec "$EXT_API" python -m py_compile /app/app/api/routes/longform.py /app/app/main.py
docker exec -i "$EXT_API" python - <<'PY'
from app.main import app
paths = {getattr(r, 'path', '') for r in app.routes}
assert '/api/longform/pricing/preview' in paths, sorted(paths)
assert '/api/longform/jobs/pricing/preview' in paths, sorted(paths)
print('PRE_RESTART_FULL_APP_IMPORT=PASS')
print('CANONICAL_PARENT_PRICING_ROUTE=PASS')
print('LEGACY_PARENT_PRICING_ALIAS=PASS')
PY

# -----------------------------------------------------------------------------
# 4. Restart only Fusion Extension API and prove health/routes.
# -----------------------------------------------------------------------------
echo "===== 4. RESTART FUSION EXTENSION API ONLY ====="
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
if [[ "$STATE" != "running" ]]; then
  docker logs "$EXT_API" --tail 80 >&2 || true
  fail "Fusion Extension API failed after canonical alias patch"
fi
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension unhealthy after alias patch: $HEALTH"
echo "FUSION_EXTENSION_HEALTH=PASS"

docker exec -i "$EXT_API" python - <<'PY'
from app.main import app
paths = {getattr(r, 'path', '') for r in app.routes}
assert '/api/longform/pricing/preview' in paths
assert '/api/longform/jobs/pricing/preview' in paths
print('LIVE_CANONICAL_PARENT_PRICING_ROUTE=PASS')
print('LIVE_LEGACY_PARENT_PRICING_ALIAS=PASS')
PY

# HTTP boundary proof: without auth both routes must be recognized (401/403/422), never 404/5xx.
for P in /api/longform/pricing/preview /api/longform/jobs/pricing/preview; do
  CODE="$(docker exec "$EXT_API" sh -lc "curl -sS -o /tmp/df-route-check.out -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8006$P || true")"
  echo "route=$P http=$CODE"
  [[ "$CODE" != "404" ]] || fail "route still returns 404: $P"
  [[ "$CODE" != "000" ]] || fail "route unreachable: $P"
  [[ "$CODE" -lt 500 ]] || fail "route returns server error: $P status=$CODE"
done

echo "DIRECTOR_PARENT_PRICING_COMPAT=PASS"

trap - ERR

echo "============================================================"
echo "MULTI_PERSON_PARENT_PRICING_404_FIX=DEPLOYED"
echo "MULTI_PERSON_FUSION_PARENT_PRICING=PASS"
echo "FUSION_EXTENSION_STATE=$STATE"
echo "FUSION_EXTENSION_HEALTH=$HEALTH"
echo "FACE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "CORE_FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "PRICING_LOGIC_TOUCH=NONE"
echo "DB_SCHEMA_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
echo "WEB_TOUCH=NONE"
echo "MULTI_PERSON_LAUNCH_HOTFIX=PASS"
echo "============================================================"
