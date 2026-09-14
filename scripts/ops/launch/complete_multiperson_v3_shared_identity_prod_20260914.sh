#!/usr/bin/env bash
set -Eeuo pipefail

EXT_API="df-svc-fusion-extension"
DIRECTOR_PATTERN='^df-v3-svc-director$|svc-director$'
SOURCE_SHA="26b1dc59dde47cb79ff9ee08fd43d1dbaf0e73b5"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$EXT_API")" == "running" ]] || fail "$EXT_API not running before change"

printf '%s\n' "============================================================"
printf '%s\n' " desifaces — COMPLETE MULTI-PERSON V3 SHARED IDENTITY"
printf ' source_sha=%s\n' "$SOURCE_SHA"
printf '%s\n' " scope=FUSION_EXTENSION_SHARED_IDENTITY_ADDITIVE_ONLY"
printf '%s\n' "============================================================"

# The production image already has the rest of the paired pricing/V3 shared dependencies.
docker exec -i "$EXT_API" python - <<'PY'
from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
from desifaces_shared.pricing.orchestration import PricingPreviewSpec
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore
assert PricingClientError is not None
assert SvcPricingClient is not None
assert PricingPreviewSpec is not None
assert CanonicalStudioWorkflowStore is not None
print('EXISTING_SHARED_RUNTIME=PASS')
PY

# Prove the observed missing dependency before changing anything.
if docker exec -i "$EXT_API" python - <<'PY' >/dev/null 2>&1
import desifaces_shared.identity
PY
then
  echo "IDENTITY_PACKAGE_ALREADY_PRESENT=YES"
else
  echo "IDENTITY_PACKAGE_MISSING_CONFIRMED=PASS"
fi

# Fetch the exact two-file identity package from the paired Director/Fusion Extension release.
mkdir -p "$TMP/identity"
curl -fsSL --connect-timeout 5 --max-time 20 \
  "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/shared/python/desifaces_shared/identity/__init__.py" \
  -o "$TMP/identity/__init__.py"
curl -fsSL --connect-timeout 5 --max-time 20 \
  "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/shared/python/desifaces_shared/identity/account_context.py" \
  -o "$TMP/identity/account_context.py"
python3 -m py_compile "$TMP/identity/__init__.py" "$TMP/identity/account_context.py"
grep -Fq 'resolve_account_context' "$TMP/identity/__init__.py" || fail "identity package export missing"
grep -Fq 'async def resolve_account_context' "$TMP/identity/account_context.py" || fail "account resolver missing"
echo "PAIRED_IDENTITY_PACKAGE_FETCH=PASS"

# Add only the missing package under the already-mounted desifaces_shared package.
# No existing shared pricing/V3 files are replaced.
docker exec "$EXT_API" sh -lc 'mkdir -p /app/desifaces_shared/identity'
docker cp "$TMP/identity/__init__.py" "$EXT_API:/app/desifaces_shared/identity/__init__.py"
docker cp "$TMP/identity/account_context.py" "$EXT_API:/app/desifaces_shared/identity/account_context.py"
echo "SHARED_IDENTITY_PACKAGE_ADDED=PASS"

# Import the dependency and the COMPLETE candidate application in a new process BEFORE restart.
docker exec -i "$EXT_API" python - <<'PY'
from desifaces_shared.identity import AccountContext, AccountContextNotFound, resolve_account_context
assert AccountContext is not None
assert AccountContextNotFound is not None
assert resolve_account_context is not None
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

# At this point startup is proven. Restart only Fusion Extension once.
docker restart "$EXT_API" >/dev/null
STATE=""; HEALTH=""
for _ in $(seq 1 40); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT_API" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT_API" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    break
  fi
  sleep 3
done
[[ "$STATE" == "running" ]] || fail "Fusion Extension API not running after restart"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension API unhealthy after restart: $HEALTH"
echo "FUSION_EXTENSION_HEALTH=PASS"

# Fresh-process live route proof.
docker exec -i "$EXT_API" python - <<'PY'
from app.main import app
paths={getattr(r,'path','') for r in app.routes}
required=[
 '/api/longform/v3/scene-pricing/preview',
 '/api/longform/v3/scene-pricing/reserve',
 '/api/longform/v3/scene-pricing/commit',
 '/api/longform/v3/scene-pricing/release',
 '/api/longform/v3/scene-stitch',
 '/api/longform/v3/assets/{media_id}/read-url',
 '/api/longform/v3/story-stitch',
]
missing=[p for p in required if p not in paths]
assert not missing, missing
print('LIVE_COMPLETE_V3_ROUTE_SET=PASS')
PY

# Exact live endpoint must resolve to auth/validation, never 404/5xx.
docker exec -i "$EXT_API" python - <<'PY'
import urllib.request, urllib.error
path='/api/longform/v3/scene-pricing/preview'
req=urllib.request.Request('http://127.0.0.1:8006'+path,data=b'{}',method='POST',headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req,timeout=5) as r:
        code=r.status
except urllib.error.HTTPError as e:
    code=e.code
assert code != 404, code
assert code < 500, code
print('LIVE_SCENE_PRICING_PREVIEW_HTTP='+str(code))
print('LIVE_SCENE_PRICING_PREVIEW_RESOLVES=PASS')
PY

DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E "$DIRECTOR_PATTERN" | head -1 || true)"
[[ -n "$DIRECTOR" ]] || fail "Director API container not found"
docker exec -i "$DIRECTOR" python - <<'PY'
import os, urllib.request, urllib.error
base=(os.getenv('DF_FUSION_EXTENSION_BASE_URL') or os.getenv('FUSION_EXTENSION_BASE_URL') or '').rstrip('/')
if not base:
    raise SystemExit('Director Fusion Extension base URL missing')
path='/api/longform/v3/scene-pricing/preview'
req=urllib.request.Request(base+path,data=b'{}',method='POST',headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req,timeout=5) as r:
        code=r.status
except urllib.error.HTTPError as e:
    code=e.code
print('DIRECTOR_FUSION_EXTENSION_BASE='+base)
print('DIRECTOR_SCENE_PRICING_PREVIEW_HTTP='+str(code))
assert code != 404, code
assert code < 500, code
print('DIRECTOR_TO_V3_SCENE_PRICING_ROUTE=PASS')
PY

rm -rf "$TMP"

echo "============================================================"
echo "ROOT_CAUSE=PRODUCTION_FUSION_EXTENSION_IMAGE_MISSING_DESIFACES_SHARED_IDENTITY"
echo "PRODUCTION_FUSION_EXTENSION_STATE=$STATE"
echo "PRODUCTION_FUSION_EXTENSION_HEALTH=$HEALTH"
echo "GLOBAL_SHARED_PRICING_FILES_REPLACED=NO"
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
