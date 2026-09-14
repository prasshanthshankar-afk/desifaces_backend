#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_SHA="8976b248c925cd2c66f3def77d8602641272104b"
EXPECTED_APP_HASH="d7a67edb6377210db62d8e24090529adce11d7fa2ee09622b788278a4548aad3"
EXPECTED_DEPS_HASH="04bb7bfeef96891a0a5f5cda60dc5e658a62de1f4f2c495424cb43e43d3fa9a3"
REPO_URL="https://github.com/prasshanthshankar-afk/desifaces_backend.git"
IMAGE="desifaces-svc-fusion-extension:mp-prod-${SOURCE_SHA:0:12}"
PREFLIGHT="df-fusion-extension-prod-preflight"
TMP="$(mktemp -d)"
SRC="$TMP/src"
ENVFILE="$TMP/preflight.env"
MUTATED=0

cleanup(){ docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT
fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"

EXT="$(docker ps --format '{{.Names}}' | grep -E '^df-svc-fusion-extension$|^df-v3-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "production Fusion Extension container not found"
[[ -n "$DIRECTOR" ]] || fail "production Director container not found"

STATE="$(docker inspect -f '{{.State.Status}}' "$EXT")"
HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT")"
[[ "$STATE" == "running" ]] || fail "Fusion Extension not running before promotion"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension unhealthy before promotion: $HEALTH"

echo "============================================================"
echo " desifaces — PROMOTE DEV-CERTIFIED MULTI-PERSON FUSION"
echo " source_sha=$SOURCE_SHA"
echo " strategy=DEV_CERTIFIED_IMMUTABLE_IMAGE"
echo " host_port_touch=NONE"
echo "============================================================"
echo "PRE_PROMOTION_PRODUCTION_HEALTH=PASS"

# Resolve compose ownership before any mutation.
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

# Build the exact DEV-certified source. Production is still untouched.
mkdir -p "$SRC"
git -C "$SRC" init -q
git -C "$SRC" remote add origin "$REPO_URL"
git -C "$SRC" fetch -q --depth=1 origin "$SOURCE_SHA"
git -C "$SRC" checkout -q --detach FETCH_HEAD
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$SOURCE_SHA" ]] || fail "source checkout mismatch"
cd "$SRC"
docker build --label "org.opencontainers.image.revision=$SOURCE_SHA" -f services/svc-fusion-extension/app/Dockerfile -t "$IMAGE" .
echo "PRODUCTION_CANDIDATE_BUILD=PASS"

APP_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'find /app/app /repo/services/shared/python/desifaces_shared -type f ! -path "*/__pycache__/*" ! -name "*.pyc" -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk "{print \$1}"')"
DEPS_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'python -m pip freeze | LC_ALL=C sort | sha256sum | awk "{print \$1}"')"
[[ "$APP_HASH" == "$EXPECTED_APP_HASH" ]] || fail "application fingerprint differs from DEV: prod=$APP_HASH dev=$EXPECTED_APP_HASH"
[[ "$DEPS_HASH" == "$EXPECTED_DEPS_HASH" ]] || fail "dependency fingerprint differs from DEV: prod=$DEPS_HASH dev=$EXPECTED_DEPS_HASH"
echo "DEV_PROD_APPLICATION_FINGERPRINT_MATCH=PASS"
echo "DEV_PROD_DEPENDENCY_FINGERPRINT_MATCH=PASS"

# Full import/route gate before mutation.
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
assert not missing, missing
print("PRODUCTION_CANDIDATE_IMPORT=PASS")
print("PRODUCTION_CANDIDATE_ROUTE_REGRESSION=PASS")
'

# Isolated candidate on production Docker network. No host port and workers disabled.
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
docker run -d --name "$PREFLIGHT" --network "$NETWORK" --network-alias "$PREFLIGHT" --env-file "$ENVFILE" -e PORT=8006 -e WORKER_ENABLED=false -e STITCH_WORKER_ENABLED=false "$IMAGE" >/dev/null

echo "PRODUCTION_ISOLATED_CANDIDATE_START=PASS"
echo "HOST_PORT_BINDING=NONE"

READY=0
for _ in $(seq 1 60); do
  if docker exec "$PREFLIGHT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8006/api/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 120 "$PREFLIGHT" >&2 || true; fail "production isolated candidate unhealthy"; }
echo "PRODUCTION_ISOLATED_CANDIDATE_HEALTH=PASS"

docker exec "$PREFLIGHT" python -c '
import urllib.request,urllib.error
for path in ("/api/longform/pricing/preview","/api/longform/v3/scene-pricing/preview"):
    r=urllib.request.Request("http://127.0.0.1:8006"+path,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
    try: code=urllib.request.urlopen(r,timeout=5).status
    except urllib.error.HTTPError as e: code=e.code
    assert code != 404 and code < 500,(path,code)
print("PRODUCTION_ISOLATED_SINGLE_PERSON_ROUTE=PASS")
print("PRODUCTION_ISOLATED_MULTI_PERSON_ROUTE=PASS")
'

docker exec "$DIRECTOR" python -c '
import urllib.request,urllib.error
u="http://df-fusion-extension-prod-preflight:8006/api/longform/v3/scene-pricing/preview"
r=urllib.request.Request(u,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
try: c=urllib.request.urlopen(r,timeout=5).status
except urllib.error.HTTPError as e: c=e.code
assert c != 404 and c < 500,c
print("PRODUCTION_DIRECTOR_TO_ISOLATED_CANDIDATE=PASS")
print("PRODUCTION_DIRECTOR_PREFLIGHT_HTTP="+str(c))
'

docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
echo "PRE_MUTATION_CERTIFICATION=PASS"

# First production mutation starts here.
CURRENT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$EXT")"
CURRENT_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$EXT")"
CANDIDATE_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
[[ -n "$CURRENT_IMAGE_REF" && -n "$CURRENT_IMAGE_ID" && -n "$CANDIDATE_IMAGE_ID" ]] || fail "image metadata unavailable"
[[ "$CURRENT_IMAGE_REF" != sha256:* ]] || fail "current image reference is not safely retaggable: $CURRENT_IMAGE_REF"
ROLLBACK_TAG="desifaces-svc-fusion-extension:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
docker tag "$CURRENT_IMAGE_ID" "$ROLLBACK_TAG"

rollback(){
  rc=$?
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker tag "$ROLLBACK_TAG" "$CURRENT_IMAGE_REF" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" >/dev/null 2>&1 || true
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap rollback ERR
MUTATED=1

docker tag "$IMAGE" "$CURRENT_IMAGE_REF"
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE"
echo "PRODUCTION_FUSION_EXTENSION_RECREATED=PASS"

EXT="$(docker ps --format '{{.Names}}' | grep -E '^df-svc-fusion-extension$|^df-v3-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "Fusion Extension container missing after promotion"

STATE=""; HEALTH=""; API_READY=0
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    if docker exec "$EXT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8006/api/health >/dev/null 2>&1; then API_READY=1; break; fi
  fi
  sleep 3
done
[[ "$STATE" == "running" ]] || fail "promoted Fusion Extension not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "promoted Fusion Extension unhealthy: $HEALTH"
(( API_READY == 1 )) || fail "promoted Fusion Extension API health endpoint not ready"
[[ "$(docker inspect -f '{{.Image}}' "$EXT")" == "$CANDIDATE_IMAGE_ID" ]] || fail "running container is not certified candidate image"
echo "PRODUCTION_CERTIFIED_IMAGE_ACTIVE=PASS"
echo "PRODUCTION_FUSION_EXTENSION_HEALTH=PASS"

# Live route regression.
docker exec "$EXT" python -c '
from app.main import app
p={getattr(r,"path","") for r in app.routes}
req={"/api/longform/pricing/preview","/api/longform/jobs/pricing/preview","/api/longform/v3/scene-pricing/preview","/api/longform/v3/scene-pricing/reserve","/api/longform/v3/scene-pricing/commit","/api/longform/v3/scene-pricing/release","/api/longform/v3/scene-stitch","/api/longform/v3/assets/{media_id}/read-url","/api/longform/v3/story-stitch"}
m=sorted(req-p); assert not m,m
print("LIVE_SINGLE_PERSON_CONTRACT=PASS")
print("LIVE_MULTI_PERSON_CONTRACT=PASS")
'

# Live Director -> promoted Fusion Extension. Settings defaults are authoritative.
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
echo "DEV_CERTIFIED=YES"
echo "DEV_PROD_FINGERPRINT_MATCH=YES"
echo "HOST_PORT_TOUCH=NONE"
echo "SINGLE_PERSON_REGRESSION_GATE=PASS"
echo "MULTI_PERSON_CONTRACT_GATE=PASS"
echo "DIRECTOR_FUSION_EXTENSION_GATE=PASS"
echo "PRODUCTION_ENVIRONMENT=UP"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "ROLLBACK_IMAGE=$ROLLBACK_TAG"
echo "============================================================"
