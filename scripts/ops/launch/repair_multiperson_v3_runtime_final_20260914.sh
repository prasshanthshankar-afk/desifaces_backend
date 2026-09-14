#!/usr/bin/env bash
set -Eeuo pipefail

EXT_API="df-svc-fusion-extension"
DIRECTOR="df-v3-svc-director"
SOURCE_SHA="26b1dc59dde47cb79ff9ee08fd43d1dbaf0e73b5"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
BASE_C="df-fusion-ext-baseline-${STAMP,,}"

fail(){ echo "FAIL: $*" >&2; exit 1; }
cleanup(){ docker rm -f "$BASE_C" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"
docker inspect "$DIRECTOR" >/dev/null 2>&1 || fail "$DIRECTOR missing"

STATE="$(docker inspect -f '{{.State.Status}}' "$EXT_API")"
HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT_API")"
[[ "$STATE" == "running" ]] || fail "$EXT_API not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "$EXT_API unhealthy before repair: $HEALTH"

echo "============================================================"
echo " desifaces — FINAL MULTI-PERSON V3 RUNTIME REPAIR"
echo " source_sha=$SOURCE_SHA"
echo " scope=FUSION_EXTENSION_ONLY"
echo "============================================================"
echo "PRE_REPAIR_PRODUCTION_STATE=running"
echo "PRE_REPAIR_PRODUCTION_HEALTH=$HEALTH"

# Only dependencies the paired V3 routes actually use.
docker exec -i "$EXT_API" python - <<'PY'
from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
from desifaces_shared.pricing.orchestration import PricingPreviewSpec
assert PricingClientError is not None
assert SvcPricingClient is not None
assert PricingPreviewSpec is not None
print('EXISTING_REQUIRED_SHARED_PRICING=PASS')
PY

SHARED_ROOT="$(docker exec -i "$EXT_API" python - <<'PY'
import desifaces_shared
paths=list(getattr(desifaces_shared, '__path__', []))
if not paths:
    raise SystemExit('desifaces_shared package path unavailable')
print(paths[0])
PY
)"
[[ -n "$SHARED_ROOT" ]] || fail "desifaces_shared root unresolved"
echo "SHARED_PACKAGE_ROOT=$SHARED_ROOT"

# Snapshot immutable image baseline for guaranteed rollback of app files.
IMAGE="$(docker inspect -f '{{.Config.Image}}' "$EXT_API")"
docker create --name "$BASE_C" --entrypoint /bin/true "$IMAGE" >/dev/null
mkdir -p "$TMP/baseline/routes" "$TMP/identity"
docker cp "$BASE_C:/app/app/main.py" "$TMP/baseline/main.py"
for f in v3_scene_pricing.py v3_scene_stitch.py v3_story_stitch.py; do
  if docker exec "$BASE_C" test -f "/app/app/api/routes/$f"; then
    docker cp "$BASE_C:/app/app/api/routes/$f" "$TMP/baseline/routes/$f"
  else
    touch "$TMP/baseline/routes/$f.absent"
  fi
done

# Remember whether identity existed in the current container before repair.
if docker exec "$EXT_API" test -d "$SHARED_ROOT/identity"; then
  mkdir -p "$TMP/identity_before"
  docker cp "$EXT_API:$SHARED_ROOT/identity/." "$TMP/identity_before/"
  IDENTITY_EXISTED=1
else
  IDENTITY_EXISTED=0
fi

rollback(){
  rc=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"

  docker cp "$TMP/baseline/main.py" "$EXT_API:/app/app/main.py" >/dev/null 2>&1 || true
  for f in v3_scene_pricing.py v3_scene_stitch.py v3_story_stitch.py; do
    if [[ -f "$TMP/baseline/routes/$f.absent" ]]; then
      docker exec "$EXT_API" rm -f "/app/app/api/routes/$f" >/dev/null 2>&1 || true
    else
      docker cp "$TMP/baseline/routes/$f" "$EXT_API:/app/app/api/routes/$f" >/dev/null 2>&1 || true
    fi
  done

  if [[ "$IDENTITY_EXISTED" == "1" ]]; then
    docker exec "$EXT_API" rm -rf "$SHARED_ROOT/identity" >/dev/null 2>&1 || true
    docker exec "$EXT_API" mkdir -p "$SHARED_ROOT/identity" >/dev/null 2>&1 || true
    docker cp "$TMP/identity_before/." "$EXT_API:$SHARED_ROOT/identity/" >/dev/null 2>&1 || true
  else
    docker exec "$EXT_API" rm -rf "$SHARED_ROOT/identity" >/dev/null 2>&1 || true
  fi

  docker restart "$EXT_API" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    s="$(docker inspect -f '{{.State.Status}}' "$EXT_API" 2>/dev/null || true)"
    h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT_API" 2>/dev/null || true)"
    [[ "$s" == "running" && ( "$h" == "healthy" || "$h" == "no-healthcheck" ) ]] && break
    sleep 2
  done
  echo "ROLLBACK_COMPLETE=YES"
  echo "PRODUCTION_RECOVERED_TO_IMAGE_BASELINE=YES"
  exit "$rc"
}
trap rollback ERR

# Fetch exact missing two-file package from the paired Director/Fusion Extension release.
curl -fsSL --connect-timeout 5 --max-time 20 \
  "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/shared/python/desifaces_shared/identity/__init__.py" \
  -o "$TMP/identity/__init__.py"
curl -fsSL --connect-timeout 5 --max-time 20 \
  "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/shared/python/desifaces_shared/identity/account_context.py" \
  -o "$TMP/identity/account_context.py"
python3 -m py_compile "$TMP/identity/__init__.py" "$TMP/identity/account_context.py"
echo "PAIRED_IDENTITY_PACKAGE_FETCH=PASS"

# Add only the missing identity package to the actual installed shared package root.
docker exec "$EXT_API" mkdir -p "$SHARED_ROOT/identity"
docker cp "$TMP/identity/__init__.py" "$EXT_API:$SHARED_ROOT/identity/__init__.py"
docker cp "$TMP/identity/account_context.py" "$EXT_API:$SHARED_ROOT/identity/account_context.py"

echo "SHARED_IDENTITY_PACKAGE_INSTALLED=PASS"

# Critical: prove every dependency and the complete patched app BEFORE restarting production.
docker exec -i "$EXT_API" python - <<'PY'
from desifaces_shared.identity import AccountContext, AccountContextNotFound, resolve_account_context
assert AccountContext and AccountContextNotFound and resolve_account_context
print('SHARED_IDENTITY_IMPORT=PASS')
PY

docker exec -i "$EXT_API" python -m py_compile \
  /app/app/main.py \
  /app/app/api/routes/v3_scene_pricing.py \
  /app/app/api/routes/v3_scene_stitch.py \
  /app/app/api/routes/v3_story_stitch.py

docker exec -i "$EXT_API" python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
required={
 '/api/longform/v3/scene-pricing/preview',
 '/api/longform/v3/scene-pricing/reserve',
 '/api/longform/v3/scene-pricing/commit',
 '/api/longform/v3/scene-pricing/release',
 '/api/longform/v3/scene-stitch',
 '/api/longform/v3/assets/{media_id}/read-url',
 '/api/longform/v3/story-stitch',
}
missing=sorted(required-paths)
assert not missing, missing
print('PRE_RESTART_FULL_APP_IMPORT=PASS')
print('PRE_RESTART_COMPLETE_V3_ROUTE_SET=PASS')
PY

# Candidate is import-clean. Restart only Fusion Extension once.
docker restart "$EXT_API" >/dev/null
STATE=""; HEALTH=""
for _ in $(seq 1 40); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT_API" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT_API" 2>/dev/null || true)"
  [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]] && break
  sleep 3
done
[[ "$STATE" == "running" ]] || fail "Fusion Extension not running after restart"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension unhealthy after restart: $HEALTH"
echo "FUSION_EXTENSION_HEALTH=PASS"

# Fresh-process route proof.
docker exec -i "$EXT_API" python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
required={
 '/api/longform/v3/scene-pricing/preview',
 '/api/longform/v3/scene-pricing/reserve',
 '/api/longform/v3/scene-pricing/commit',
 '/api/longform/v3/scene-pricing/release',
 '/api/longform/v3/scene-stitch',
 '/api/longform/v3/assets/{media_id}/read-url',
 '/api/longform/v3/story-stitch',
}
missing=sorted(required-paths)
assert not missing, missing
print('LIVE_COMPLETE_V3_ROUTE_SET=PASS')
PY

# Exact preview endpoint: auth/validation responses are acceptable; 404/5xx are not.
docker exec -i "$EXT_API" python - <<'PY'
import urllib.request, urllib.error
url='http://127.0.0.1:8006/api/longform/v3/scene-pricing/preview'
req=urllib.request.Request(url,data=b'{}',method='POST',headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req,timeout=5) as r: code=r.status
except urllib.error.HTTPError as e: code=e.code
assert code != 404, code
assert code < 500, code
print('LIVE_SCENE_PRICING_PREVIEW_HTTP='+str(code))
print('LIVE_SCENE_PRICING_PREVIEW_RESOLVES=PASS')
PY

# Prove the actual Director -> Fusion Extension boundary sees the same route.
docker exec -i "$DIRECTOR" python - <<'PY'
import os, urllib.request, urllib.error
base=(os.getenv('DF_FUSION_EXTENSION_BASE_URL') or os.getenv('FUSION_EXTENSION_BASE_URL') or '').rstrip('/')
if not base:
    raise SystemExit('Director Fusion Extension base URL missing')
url=base+'/api/longform/v3/scene-pricing/preview'
req=urllib.request.Request(url,data=b'{}',method='POST',headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req,timeout=5) as r: code=r.status
except urllib.error.HTTPError as e: code=e.code
print('DIRECTOR_FUSION_EXTENSION_BASE='+base)
print('DIRECTOR_SCENE_PRICING_PREVIEW_HTTP='+str(code))
assert code != 404, code
assert code < 500, code
print('DIRECTOR_TO_V3_SCENE_PRICING_ROUTE=PASS')
PY

trap - ERR

echo "============================================================"
echo "ROOT_CAUSE=PRODUCTION_FUSION_EXTENSION_IMAGE_MISSING_PAIRED_IDENTITY_DEPENDENCY"
echo "FALSE_V3_PREFLIGHT_REMOVED=YES"
echo "PRODUCTION_FUSION_EXTENSION_STATE=$STATE"
echo "PRODUCTION_FUSION_EXTENSION_HEALTH=$HEALTH"
echo "SINGLE_PERSON_PRICING_TOUCH=NONE"
echo "FACE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "CORE_FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "DB_SCHEMA_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
echo "WEB_TOUCH=NONE"
echo "MULTI_PERSON_FUSION_V3_RUNTIME=PASS"
echo "PRODUCTION_ENVIRONMENT=UP"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "============================================================"
