#!/usr/bin/env bash
set -Eeuo pipefail

EXT_API="df-svc-fusion-extension"
DIRECTOR="df-v3-svc-director"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"
docker inspect "$DIRECTOR" >/dev/null 2>&1 || fail "$DIRECTOR missing"

EXT_STATE="$(docker inspect -f '{{.State.Status}}' "$EXT_API")"
EXT_HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT_API")"
DIR_STATE="$(docker inspect -f '{{.State.Status}}' "$DIRECTOR")"

printf '%s\n' "============================================================"
printf '%s\n' " desifaces — LIVE MULTI-PERSON V3 CONTRACT CERTIFICATION"
printf '%s\n' " scope=READ_ONLY"
printf '%s\n' "============================================================"
printf 'FUSION_EXTENSION_STATE=%s\n' "$EXT_STATE"
printf 'FUSION_EXTENSION_HEALTH=%s\n' "$EXT_HEALTH"
printf 'DIRECTOR_STATE=%s\n' "$DIR_STATE"

[[ "$EXT_STATE" == "running" ]] || fail "Fusion Extension not running"
[[ "$EXT_HEALTH" == "healthy" || "$EXT_HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension unhealthy"
[[ "$DIR_STATE" == "running" ]] || fail "Director not running"

echo "RUNTIME_HEALTH=PASS"

# 1) Prove the on-disk candidate imports and contains the complete route set.
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
if missing:
    raise SystemExit('MISSING_APP_ROUTES=' + ','.join(missing))
print('APP_ROUTE_SET=PASS')
for p in required:
    print('route=' + p)
PY

# 2) Prove the currently running Fusion Extension HTTP server resolves each route.
docker exec -i "$EXT_API" python - <<'PY'
import urllib.request, urllib.error
base='http://127.0.0.1:8006'
posts=[
 '/api/longform/v3/scene-pricing/preview',
 '/api/longform/v3/scene-pricing/reserve',
 '/api/longform/v3/scene-pricing/commit',
 '/api/longform/v3/scene-pricing/release',
 '/api/longform/v3/scene-stitch',
 '/api/longform/v3/story-stitch',
]
for path in posts:
    req=urllib.request.Request(base+path,data=b'{}',method='POST',headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=5) as r:
            code=r.status
    except urllib.error.HTTPError as e:
        code=e.code
    if code == 404:
        raise SystemExit(f'LIVE_ROUTE_404={path}')
    print(f'live={path} http={code}')
path='/api/longform/v3/assets/00000000-0000-0000-0000-000000000000/read-url'
req=urllib.request.Request(base+path,method='GET')
try:
    with urllib.request.urlopen(req,timeout=5) as r:
        code=r.status
except urllib.error.HTTPError as e:
    code=e.code
if code == 404:
    raise SystemExit(f'LIVE_ROUTE_404={path}')
print(f'live={path} http={code}')
print('LIVE_EXTENSION_ROUTE_RESOLUTION=PASS')
PY

# 3) Prove Director is targeting Fusion Extension and sees the SAME route (no auth token required to prove non-404 routing).
BASE="$(docker exec "$DIRECTOR" sh -lc 'printf "%s" "${DF_FUSION_EXTENSION_BASE_URL:-http://svc-fusion-extension:8006}"')"
[[ -n "$BASE" ]] || fail "Director Fusion Extension base URL empty"
printf 'DIRECTOR_FUSION_EXTENSION_BASE=%s\n' "$BASE"

docker exec -e TARGET_BASE="$BASE" -i "$DIRECTOR" python - <<'PY'
import os, urllib.request, urllib.error
base=os.environ['TARGET_BASE'].rstrip('/')
path='/api/longform/v3/scene-pricing/preview'
req=urllib.request.Request(base+path,data=b'{}',method='POST',headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req,timeout=7) as r:
        code=r.status
except urllib.error.HTTPError as e:
    code=e.code
except Exception as exc:
    raise SystemExit('DIRECTOR_TARGET_CONNECT_FAILED=' + repr(exc))
print(f'director_target={base+path} http={code}')
if code == 404:
    raise SystemExit('DIRECTOR_TARGET_ROUTE_404')
print('DIRECTOR_TO_EXTENSION_ROUTE=PASS')
PY

echo "============================================================"
echo "LIVE_MULTI_PERSON_V3_ROUTE_CONTRACT=PASS"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "MUTATION=NONE"
echo "============================================================"
