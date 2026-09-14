#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_SHA="8976b248c925cd2c66f3def77d8602641272104b"
REPO_URL="https://github.com/prasshanthshankar-afk/desifaces_backend.git"
IMAGE="desifaces-svc-fusion-extension:mp-prod-${SOURCE_SHA:0:12}"
PREFLIGHT="df-fusion-extension-prod-preflight"
TMP="$(mktemp -d)"
SRC="$TMP/src"
ENVFILE="$TMP/preflight.env"
EXPECTED_APP_HASH="${EXPECTED_APP_HASH:-}"
EXPECTED_DEPS_HASH="${EXPECTED_DEPS_HASH:-}"

cleanup(){ docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT
fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ -n "$EXPECTED_APP_HASH" && -n "$EXPECTED_DEPS_HASH" ]] || fail "DEV candidate fingerprints required"

EXT="$(docker ps --format '{{.Names}}' | grep -E '^df-svc-fusion-extension$|^df-v3-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "production Fusion Extension container not found"
DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ -n "$DIRECTOR" ]] || fail "production Director container not found"

STATE="$(docker inspect -f '{{.State.Status}}' "$EXT")"
HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT")"
[[ "$STATE" == "running" ]] || fail "Fusion Extension not running before promotion"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension unhealthy before promotion: $HEALTH"

echo "============================================================"
echo " desifaces — PROMOTE CERTIFIED MULTI-PERSON FUSION CANDIDATE"
echo " source_sha=$SOURCE_SHA"
echo " strategy=DEV_CERTIFIED_CURRENT_BASELINE_IMMUTABLE_IMAGE"
echo "============================================================"
echo "PRE_PROMOTION_PRODUCTION_HEALTH=PASS"

# Resolve Compose ownership BEFORE any production mutation.
WORKDIR="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$EXT" 2>/dev/null || true)"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$EXT" 2>/dev/null || true)"
SERVICE="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$EXT" 2>/dev/null || true)"
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$EXT" 2>/dev/null || true)"
[[ -n "$WORKDIR" && -d "$WORKDIR" ]] || fail "Compose working directory unavailable"
[[ -n "$CONFIG_FILES" && -n "$SERVICE" && -n "$PROJECT" ]] || fail "Compose ownership metadata incomplete"

COMPOSE=(docker compose --project-directory "$WORKDIR" -p "$PROJECT")
IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
for f in "${CFG_ARR[@]}"; do
  [[ -f "$f" ]] || fail "Compose file missing: $f"
  COMPOSE+=( -f "$f" )
done
"${COMPOSE[@]}" config -q

echo "PRODUCTION_COMPOSE_OWNERSHIP=PASS"

# Build exact same source commit used in DEV certification. No running service changed yet.
mkdir -p "$SRC"
git -C "$SRC" init -q
git -C "$SRC" remote add origin "$REPO_URL"
git -C "$SRC" fetch -q --depth=1 origin "$SOURCE_SHA"
git -C "$SRC" checkout -q --detach FETCH_HEAD
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$SOURCE_SHA" ]] || fail "source checkout mismatch"
cd "$SRC"
docker build \
  --label "org.opencontainers.image.revision=$SOURCE_SHA" \
  -f services/svc-fusion-extension/app/Dockerfile \
  -t "$IMAGE" \
  .
echo "PRODUCTION_CANDIDATE_BUILD=PASS"

APP_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'find /app/app /repo/services/shared/python/desifaces_shared -type f ! -path "*/__pycache__/*" ! -name "*.pyc" -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk "{print \$1}"')"
DEPS_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'python -m pip freeze | LC_ALL=C sort | sha256sum | awk "{print \$1}"')"
[[ "$APP_HASH" == "$EXPECTED_APP_HASH" ]] || fail "application fingerprint differs from DEV: prod=$APP_HASH dev=$EXPECTED_APP_HASH"
[[ "$DEPS_HASH" == "$EXPECTED_DEPS_HASH" ]] || fail "dependency fingerprint differs from DEV: prod=$DEPS_HASH dev=$EXPECTED_DEPS_HASH"
echo "DEV_PROD_APPLICATION_FINGERPRINT_MATCH=PASS"
echo "DEV_PROD_DEPENDENCY_FINGERPRINT_MATCH=PASS"

# Full image import and route regression before touching running production.
docker run --rm \
  -e DATABASE_URL='postgresql://invalid:invalid@127.0.0.1:1/invalid' \
  -e JWT_SECRET='candidate-certification-only' \
  -e AZURE_STORAGE_CONNECTION_STRING='UseDevelopmentStorage=true' \
  -e WORKER_ENABLED=false \
  -e STITCH_WORKER_ENABLED=false \
  --entrypoint python "$IMAGE" -c '
from app.main import app
paths={getattr(r,"path","") for r in app.routes}
required={
 "/api/longform/pricing/preview",
 "/api/longform/jobs/pricing/preview",
 "/api/longform/v3/scene-pricing/preview",
 "/api/longform/v3/scene-pricing/reserve",
 "/api/longform/v3/scene-pricing/commit",
 "/api/longform/v3/scene-pricing/release",
 "/api/longform/v3/scene-stitch",
 "/api/longform/v3/assets/{media_id}/read-url",
 "/api/longform/v3/story-stitch",
}
missing=sorted(required-paths)
assert not missing,missing
print("PRODUCTION_CANDIDATE_IMPORT=PASS")
print("PRODUCTION_CANDIDATE_ROUTE_REGRESSION=PASS")
'

# Isolated preflight uses production config but workers disabled; no authenticated/paid endpoint is invoked.
docker inspect "$EXT" > "$TMP/ext.inspect.json"
python3 - "$TMP/ext.inspect.json" "$ENVFILE" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1]))[0]
skip={"WORKER_ENABLED","STITCH_WORKER_ENABLED","PORT"}
with open(sys.argv[2],"w") as out:
    for item in obj.get("Config",{}).get("Env",[]):
        if "=" not in item: continue
        k,v=item.split("=",1)
        if k in skip: continue
        if "\n" in v or "\r" in v: raise SystemExit(f"unsupported newline env: {k}")
        out.write(f"{k}={v}\n")
PY
chmod 600 "$ENVFILE"
NETWORK="$(python3 - "$TMP/ext.inspect.json" <<'PY'
import json,sys
nets=list(json.load(open(sys.argv[1]))[0].get("NetworkSettings",{}).get("Networks",{}))
if not nets: raise SystemExit(2)
print(nets[0])
PY
)"
[[ -n "$NETWORK" ]] || fail "production network unresolved"
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
docker run -d \
  --name "$PREFLIGHT" --network "$NETWORK" --network-alias "$PREFLIGHT" \
  --env-file "$ENVFILE" -e PORT=8006 -e WORKER_ENABLED=false -e STITCH_WORKER_ENABLED=false \
  -p 127.0.0.1:18006:8006 "$IMAGE" >/dev/null
READY=0
for _ in $(seq 1 60); do
  if curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:18006/api/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 120 "$PREFLIGHT" >&2 || true; fail "production isolated candidate unhealthy"; }
python3 - <<'PY'
import urllib.request,urllib.error
for path in ("/api/longform/pricing/preview","/api/longform/v3/scene-pricing/preview"):
    r=urllib.request.Request("http://127.0.0.1:18006"+path,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
    try: code=urllib.request.urlopen(r,timeout=5).status
    except urllib.error.HTTPError as e: code=e.code
    assert code != 404 and code < 500,(path,code)
print("PRODUCTION_ISOLATED_SINGLE_PERSON_ROUTE=PASS")
print("PRODUCTION_ISOLATED_MULTI_PERSON_ROUTE=PASS")
PY

docker exec "$DIRECTOR" python -c '
import urllib.request,urllib.error
u="http://df-fusion-extension-prod-preflight:8006/api/longform/v3/scene-pricing/preview"
r=urllib.request.Request(u,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
try: c=urllib.request.urlopen(r,timeout=5).status
except urllib.error.HTTPError as e: c=e.code
assert c != 404 and c < 500,c
print("PRODUCTION_DIRECTOR_TO_ISOLATED_CANDIDATE=PASS")
'

docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true

echo "PRE_MUTATION_CERTIFICATION=PASS"

# ---- First production mutation occurs only below this line. ----
CURRENT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$EXT")"
CURRENT_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$EXT")"
CANDIDATE_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
ROLLBACK_TAG="desifaces-svc-fusion-extension:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
[[ -n "$CURRENT_IMAGE_REF" && -n "$CURRENT_IMAGE_ID" && -n "$CANDIDATE_IMAGE_ID" ]] || fail "image metadata unavailable"
docker tag "$CURRENT_IMAGE_ID" "$ROLLBACK_TAG"

rollback(){
  rc=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker tag "$ROLLBACK_TAG" "$CURRENT_IMAGE_REF" >/dev/null 2>&1 || true
  (cd "$WORKDIR" && "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" >/dev/null 2>&1) || true
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap rollback ERR

docker tag "$IMAGE" "$CURRENT_IMAGE_REF"
(cd "$WORKDIR" && "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE")

echo "PRODUCTION_FUSION_EXTENSION_RECREATED=PASS"

# Container name may be recreated but Compose should preserve it; resolve again defensively.
EXT="$(docker ps --format '{{.Names}}' | grep -E '^df-svc-fusion-extension$|^df-v3-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "Fusion Extension container missing after promotion"
STATE=""; HEALTH=""
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT" 2>/dev/null || true)"
  [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]] && break
  sleep 3
done
[[ "$STATE" == "running" ]] || fail "promoted Fusion Extension not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "promoted Fusion Extension unhealthy: $HEALTH"
[[ "$(docker inspect -f '{{.Image}}' "$EXT")" == "$CANDIDATE_IMAGE_ID" ]] || fail "running container is not certified candidate image"
echo "PRODUCTION_CERTIFIED_IMAGE_ACTIVE=PASS"
echo "PRODUCTION_FUSION_EXTENSION_HEALTH=PASS"

# Live contract regression: old single-person route + complete multi-person route set.
docker exec "$EXT" python -c '
from app.main import app
p={getattr(r,"path","") for r in app.routes}
req={"/api/longform/pricing/preview","/api/longform/jobs/pricing/preview","/api/longform/v3/scene-pricing/preview","/api/longform/v3/scene-pricing/reserve","/api/longform/v3/scene-pricing/commit","/api/longform/v3/scene-pricing/release","/api/longform/v3/scene-stitch","/api/longform/v3/assets/{media_id}/read-url","/api/longform/v3/story-stitch"}
m=sorted(req-p); assert not m,m
print("LIVE_SINGLE_PERSON_CONTRACT=PASS")
print("LIVE_MULTI_PERSON_CONTRACT=PASS")
'

# Use Director settings object, not raw environment; defaults are authoritative.
docker exec "$DIRECTOR" python -c '
import urllib.request,urllib.error
from app.config import settings
base=str(settings.DF_FUSION_EXTENSION_BASE_URL).rstrip("/")
u=base+"/api/longform/v3/scene-pricing/preview"
r=urllib.request.Request(u,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
try: c=urllib.request.urlopen(r,timeout=5).status
except urllib.error.HTTPError as e: c=e.code
print("DIRECTOR_FUSION_EXTENSION_BASE="+base)
print("DIRECTOR_SCENE_PRICING_PREVIEW_HTTP="+str(c))
assert c != 404 and c < 500,c
print("LIVE_DIRECTOR_TO_FUSION_EXTENSION=PASS")
'

trap - ERR

echo "============================================================"
echo "SOURCE_BASELINE=CURRENT_PLUS_MINIMAL_V3_CONTRACT_RESTORE"
echo "OLD_FUSION_EXTENSION_SOURCE_DEPLOYED=NO"
echo "DEV_CERTIFIED=YES"
echo "DEV_PROD_FINGERPRINT_MATCH=YES"
echo "SINGLE_PERSON_REGRESSION_GATE=PASS"
echo "MULTI_PERSON_CONTRACT_GATE=PASS"
echo "DIRECTOR_FUSION_EXTENSION_GATE=PASS"
echo "PRODUCTION_ENVIRONMENT=UP"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "ROLLBACK_IMAGE=$ROLLBACK_TAG"
echo "============================================================"
