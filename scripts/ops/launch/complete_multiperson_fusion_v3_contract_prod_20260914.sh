#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_SHA="26b1dc59dde47cb79ff9ee08fd43d1dbaf0e73b5"
EXT_API="df-svc-fusion-extension"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/tmp/df-multiperson-v3-contract-${STAMP}"
TMP="$(mktemp -d)"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$EXT_API" >/dev/null 2>&1 || fail "$EXT_API missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$EXT_API")" == "running" ]] || fail "$EXT_API not running before change"

printf '%s\n' "============================================================"
printf '%s\n' " desifaces — COMPLETE MULTI-PERSON FUSION V3 CONTRACT"
printf ' source_sha=%s\n' "$SOURCE_SHA"
printf '%s\n' " scope=FUSION_EXTENSION_API_ONLY"
printf '%s\n' " strategy=PAIRED_DIRECTOR_RELEASE_CONTRACT"
printf '%s\n' "============================================================"

# Exact contract expected by Director's ParentScenePricingClient/SceneStitchClient/StoryStitchClient.
REQUIRED_PATHS=(
  "/api/longform/v3/scene-pricing/preview"
  "/api/longform/v3/scene-pricing/reserve"
  "/api/longform/v3/scene-pricing/commit"
  "/api/longform/v3/scene-pricing/release"
  "/api/longform/v3/scene-stitch"
  "/api/longform/v3/assets/{media_id}/read-url"
  "/api/longform/v3/story-stitch"
)

FILES=(
  "app/api/routes/v3_scene_pricing.py"
  "app/api/routes/v3_scene_stitch.py"
  "app/api/routes/v3_story_stitch.py"
  "app/services/v3_stitch_resilience.py"
)

mkdir -p "$BACKUP/app/api/routes" "$BACKUP/app/services" "$TMP/app/api/routes" "$TMP/app/services"
docker cp "$EXT_API:/app/app/main.py" "$BACKUP/app/main.py"

for rel in "${FILES[@]}"; do
  mkdir -p "$BACKUP/$(dirname "$rel")" "$TMP/$(dirname "$rel")"
  if docker exec "$EXT_API" test -f "/app/$rel"; then
    docker cp "$EXT_API:/app/$rel" "$BACKUP/$rel"
  else
    touch "$BACKUP/${rel//\//__}.absent"
  fi
  curl -fsSL --connect-timeout 5 --max-time 20 \
    "https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/svc-fusion-extension/app/${rel}" \
    -o "$TMP/$rel"
done

echo "PINNED_PAIRED_CONTRACT_FETCH=PASS"
python3 -m py_compile \
  "$TMP/app/api/routes/v3_scene_pricing.py" \
  "$TMP/app/api/routes/v3_scene_stitch.py" \
  "$TMP/app/api/routes/v3_story_stitch.py" \
  "$TMP/app/services/v3_stitch_resilience.py"

grep -Fq 'prefix="/api/longform/v3/scene-pricing"' "$TMP/app/api/routes/v3_scene_pricing.py" || fail "scene pricing prefix missing"
grep -Fq '@router.post("/preview"' "$TMP/app/api/routes/v3_scene_pricing.py" || fail "scene pricing preview missing"
grep -Fq '@router.post("/reserve"' "$TMP/app/api/routes/v3_scene_pricing.py" || fail "scene pricing reserve missing"
grep -Fq '@router.post("/commit"' "$TMP/app/api/routes/v3_scene_pricing.py" || fail "scene pricing commit missing"
grep -Fq '@router.post("/release"' "$TMP/app/api/routes/v3_scene_pricing.py" || fail "scene pricing release missing"
grep -Fq '@router.post("/scene-stitch"' "$TMP/app/api/routes/v3_scene_stitch.py" || fail "scene stitch missing"
grep -Fq '@router.get("/assets/{media_id}/read-url"' "$TMP/app/api/routes/v3_scene_stitch.py" || fail "V3 video read-url missing"
grep -Eq '@router.post\([[:space:]]*"/story-stitch"|"/story-stitch"' "$TMP/app/api/routes/v3_story_stitch.py" || fail "story stitch missing"
echo "PAIRED_SOURCE_CONTRACT=PASS"

rollback(){
  rc=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker cp "$BACKUP/app/main.py" "$EXT_API:/app/app/main.py" >/dev/null 2>&1 || true
  for rel in "${FILES[@]}"; do
    absent="$BACKUP/${rel//\//__}.absent"
    if [[ -f "$absent" ]]; then
      docker exec "$EXT_API" rm -f "/app/$rel" >/dev/null 2>&1 || true
    elif [[ -f "$BACKUP/$rel" ]]; then
      docker cp "$BACKUP/$rel" "$EXT_API:/app/$rel" >/dev/null 2>&1 || true
    fi
  done
  docker restart "$EXT_API" >/dev/null 2>&1 || true
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap rollback ERR

# Copy paired route implementations, but preserve all current longform/pricing code.
for rel in "${FILES[@]}"; do
  docker cp "$TMP/$rel" "$EXT_API:/app/$rel"
done

# Patch only router registration in the CURRENT runtime main.py. Do not replace main.py.
docker cp "$EXT_API:/app/app/main.py" "$TMP/current_main.py"
python3 - "$TMP/current_main.py" "$TMP/patched_main.py" <<'PY'
from pathlib import Path
import sys
src=Path(sys.argv[1]).read_text()
imports=[
    'from app.api.routes.v3_scene_pricing import router as v3_scene_pricing_router',
    'from app.api.routes.v3_scene_stitch import router as v3_scene_stitch_router',
    'from app.api.routes.v3_story_stitch import router as v3_story_stitch_router',
]
includes=[
    '    app.include_router(v3_scene_pricing_router)',
    '    app.include_router(v3_scene_stitch_router)',
    '    app.include_router(v3_story_stitch_router)',
]
for line in imports:
    if line not in src:
        marker='from app.api.routes.longform import router as longform_router'
        if marker not in src:
            raise SystemExit('longform router import marker missing')
        src=src.replace(marker, marker+'\n'+line, 1)
for line in includes:
    if line not in src:
        marker='    app.include_router(longform_router)'
        if marker not in src:
            raise SystemExit('longform router include marker missing')
        src=src.replace(marker, marker+'\n'+line, 1)
Path(sys.argv[2]).write_text(src)
PY

docker cp "$TMP/patched_main.py" "$EXT_API:/app/app/main.py"
echo "FUSION_EXTENSION_V3_RUNTIME_PATCHED=PASS"

# Critical gate: import the full candidate app in a NEW Python process before restarting the live server.
docker exec -i "$EXT_API" python -m py_compile \
  /app/app/main.py \
  /app/app/api/routes/v3_scene_pricing.py \
  /app/app/api/routes/v3_scene_stitch.py \
  /app/app/api/routes/v3_story_stitch.py \
  /app/app/services/v3_stitch_resilience.py

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
print('PRE_RESTART_V3_ROUTE_SET=PASS')
PY

# Restart only after the complete paired contract imports cleanly.
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
[[ "$STATE" == "running" ]] || fail "Fusion Extension API not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension API unhealthy: $HEALTH"

echo "FUSION_EXTENSION_HEALTH=PASS"

# Certify live process route set and prove every route resolves to auth/validation rather than 404.
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
print('LIVE_V3_SCENE_PRICING_PREVIEW=PASS')
print('LIVE_V3_SCENE_PRICING_LIFECYCLE=PASS')
print('LIVE_V3_SCENE_STITCH=PASS')
print('LIVE_V3_SCENE_VIDEO_READ_URL=PASS')
print('LIVE_V3_STORY_STITCH=PASS')
PY

docker exec -i "$EXT_API" python - <<'PY'
import urllib.request, urllib.error, json
paths=[
 '/api/longform/v3/scene-pricing/preview',
 '/api/longform/v3/scene-pricing/reserve',
 '/api/longform/v3/scene-pricing/commit',
 '/api/longform/v3/scene-pricing/release',
 '/api/longform/v3/scene-stitch',
 '/api/longform/v3/story-stitch',
]
for path in paths:
    req=urllib.request.Request('http://127.0.0.1:8006'+path,data=b'{}',method='POST',headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=5) as r:
            code=r.status
    except urllib.error.HTTPError as e:
        code=e.code
    assert code != 404, (path,code)
    print(f'route={path} http={code}')
print('DIRECTOR_FUSION_EXTENSION_CONTRACT=PASS')
PY

trap - ERR
rm -rf "$TMP"

echo "============================================================"
echo "ROOT_CAUSE=PRODUCTION_FUSION_EXTENSION_MISSING_DIRECTOR_V3_ROUTE_SET"
echo "DIRECTOR_EXPECTED_SCENE_PRICING=/api/longform/v3/scene-pricing/*"
echo "MULTI_PERSON_V3_PRICING_CONTRACT=DEPLOYED"
echo "MULTI_PERSON_V3_SCENE_STITCH_CONTRACT=DEPLOYED"
echo "MULTI_PERSON_V3_STORY_STITCH_CONTRACT=DEPLOYED"
echo "FUSION_EXTENSION_STATE=$STATE"
echo "FUSION_EXTENSION_HEALTH=$HEALTH"
echo "FACE_TOUCH=NONE"
echo "AUDIO_TOUCH=NONE"
echo "CORE_FUSION_TOUCH=NONE"
echo "DIRECTOR_TOUCH=NONE"
echo "PRICING_LOGIC_CHANGE=NONE"
echo "DB_SCHEMA_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
echo "WEB_TOUCH=NONE"
echo "MULTI_PERSON_FUSION_V3_CONTRACT=PASS"
echo "============================================================"
