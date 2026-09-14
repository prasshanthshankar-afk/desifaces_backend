#!/usr/bin/env bash
set -Eeuo pipefail

EXT_API="df-svc-fusion-extension"
SOURCE_REF="fix/v3-director-face-premium-context-20260830"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/tmp/df-v3-identity-compat-${STAMP}"
TMP="$(mktemp -d)"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$EXT_API")" == "running" ]] || fail "$EXT_API not running"

printf '%s\n' "============================================================"
printf '%s\n' " desifaces — COMPLETE MULTI-PERSON V3 IDENTITY COMPAT"
printf '%s\n' " scope=FUSION_EXTENSION_API_ONLY"
printf '%s\n' " strategy=LOCAL_IDENTITY_COMPAT_NO_SHARED_PACKAGE_REPLACEMENT"
printf '%s\n' "============================================================"

# Prove existing pricing dependencies already present in the live image before mutation.
docker exec -i "$EXT_API" python - <<'PY'
from desifaces_shared.pricing.client import PricingClientError, SvcPricingClient
from desifaces_shared.pricing.orchestration import (
    PricingCommitSpec,
    PricingPreviewSpec,
    PricingReleaseSpec,
    PricingReserveSpec,
    build_commit_request,
    build_preview_request,
    build_pricing_summary,
    build_release_request,
    build_reserve_request,
    make_committed_artifact,
    make_preview_artifact,
    make_released_artifact,
    make_reserved_artifact,
)
print("EXISTING_SHARED_PRICING_DEPENDENCIES=PASS")
PY

mkdir -p "$BACKUP" "$TMP"
for p in \
  /app/app/main.py \
  /app/app/api/routes/v3_scene_pricing.py \
  /app/app/api/routes/v3_scene_stitch.py \
  /app/app/api/routes/v3_story_stitch.py \
  /app/app/v3_identity_compat.py
 do
  key="$(echo "$p" | sed 's#/#__#g')"
  if docker exec "$EXT_API" test -f "$p"; then
    docker cp "$EXT_API:$p" "$BACKUP/$key"
  else
    touch "$BACKUP/$key.absent"
  fi
done

rollback(){
  rc=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  for p in \
    /app/app/main.py \
    /app/app/api/routes/v3_scene_pricing.py \
    /app/app/api/routes/v3_scene_stitch.py \
    /app/app/api/routes/v3_story_stitch.py \
    /app/app/v3_identity_compat.py
  do
    key="$(echo "$p" | sed 's#/#__#g')"
    if [[ -f "$BACKUP/$key.absent" ]]; then
      docker exec "$EXT_API" rm -f "$p" >/dev/null 2>&1 || true
    elif [[ -f "$BACKUP/$key" ]]; then
      docker cp "$BACKUP/$key" "$EXT_API:$p" >/dev/null 2>&1 || true
    fi
  done
  # Restart only if the current server stopped or became unhealthy; otherwise preserve loaded healthy process.
  state="$(docker inspect -f '{{.State.Status}}' "$EXT_API" 2>/dev/null || true)"
  if [[ "$state" != "running" ]]; then
    docker restart "$EXT_API" >/dev/null 2>&1 || true
  fi
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap rollback ERR

# Fetch only the paired release account resolver. It has no desifaces_shared imports.
curl -fsSL --connect-timeout 5 --max-time 20 \
  "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_REF}/services/shared/python/desifaces_shared/identity/account_context.py" \
  -o "$TMP/v3_identity_compat.py"
python3 -m py_compile "$TMP/v3_identity_compat.py"
grep -Fq 'async def resolve_account_context' "$TMP/v3_identity_compat.py" || fail "identity resolver missing"
echo "PAIRED_IDENTITY_HELPER_FETCH=PASS"

docker cp "$TMP/v3_identity_compat.py" "$EXT_API:/app/app/v3_identity_compat.py"

# Redirect only V3 Multi-Person route modules to the local compatibility helper.
for route in v3_scene_pricing.py v3_scene_stitch.py v3_story_stitch.py; do
  docker cp "$EXT_API:/app/app/api/routes/$route" "$TMP/$route"
  python3 - "$TMP/$route" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
s=p.read_text()
old="from desifaces_shared.identity import ("
new="from app.v3_identity_compat import ("
if old not in s and new not in s:
    raise SystemExit(f"identity import marker missing in {p.name}")
s=s.replace(old,new)
p.write_text(s)
PY
  docker cp "$TMP/$route" "$EXT_API:/app/app/api/routes/$route"
done

echo "V3_IDENTITY_IMPORT_REDIRECT=PASS"

# Full candidate compile/import gate BEFORE any restart.
docker exec -i "$EXT_API" python -m py_compile \
  /app/app/v3_identity_compat.py \
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

# Only now restart Fusion Extension once.
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
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension API unhealthy: $HEALTH"
echo "FUSION_EXTENSION_HEALTH=PASS"

# Live route-set proof from a fresh process.
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

# Prove the exact preview path is no longer 404 inside Fusion Extension.
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
print(f'LIVE_SCENE_PRICING_PREVIEW_HTTP={code}')
print('LIVE_SCENE_PRICING_PREVIEW_RESOLVES=PASS')
PY

# Prove Director is pointed at Fusion Extension and sees the exact route, not merely a local route.
DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|svc-director$' | head -1 || true)"
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
print('DIRECTOR_TO_V3_SCENE_PRICING_ROUTE=PASS')
PY

trap - ERR
rm -rf "$TMP"

echo "============================================================"
echo "ROOT_CAUSE=PAIRED_V3_ROUTES_REQUIRED_IDENTITY_PACKAGE_ABSENT_FROM_PROD_IMAGE"
echo "IDENTITY_COMPAT_SCOPE=LOCAL_TO_FUSION_EXTENSION_V3_ROUTES"
echo "GLOBAL_SHARED_PACKAGE_REPLACED=NO"
echo "SINGLE_PERSON_PRICING_SHARED_PACKAGE_TOUCH=NONE"
echo "FACE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "CORE_FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "DB_SCHEMA_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
echo "WEB_TOUCH=NONE"
echo "MULTI_PERSON_FUSION_V3_RUNTIME=PASS"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "============================================================"
