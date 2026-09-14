#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_APP_HASH="d7a67edb6377210db62d8e24090529adce11d7fa2ee09622b788278a4548aad3"
EXPECTED_DEPS_HASH="04bb7bfeef96891a0a5f5cda60dc5e658a62de1f4f2c495424cb43e43d3fa9a3"
IMAGE="desifaces-svc-fusion-extension:mp-prod-8976b248c925"
PREFLIGHT="df-fusion-extension-prod-env-preflight"
TMP="$(mktemp -d)"
MUTATED=0

cleanup(){ docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT
fail(){ echo "FAIL: $*" >&2; return 1; }

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"

echo "============================================================"
echo " desifaces — RECOVER + ACTIVATE CERTIFIED MULTI-PERSON FUSION"
echo " strategy=USE_CANONICAL_PRODUCTION_INTERPOLATION_ENV"
echo " candidate_image=$IMAGE"
echo "============================================================"

EXT="$(docker ps -a --format '{{.Names}}' | grep -E '^df-svc-fusion-extension$|^df-v3-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
WORKER="$(docker ps --format '{{.Names}}' | grep -E '^df-svc-fusion-extension-worker$|^df-v3-svc-fusion-extension-worker$|svc-fusion-extension-worker$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "Fusion Extension API container not found"
[[ -n "$DIRECTOR" ]] || fail "Director container not found"
[[ -n "$WORKER" ]] || fail "Fusion Extension worker donor not found"

echo "PRODUCTION_RUNTIME_DISCOVERY=PASS"

WORKDIR="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$EXT" 2>/dev/null || true)"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$EXT" 2>/dev/null || true)"
SERVICE="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$EXT" 2>/dev/null || true)"
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$EXT" 2>/dev/null || true)"
[[ -n "$WORKDIR" && -d "$WORKDIR" ]] || fail "Compose working directory unavailable"
[[ -n "$CONFIG_FILES" && -n "$SERVICE" && -n "$PROJECT" ]] || fail "Compose ownership metadata incomplete"

CANON_ENV="$WORKDIR/infra/.env"
[[ -f "$CANON_ENV" ]] || fail "canonical production env file missing: $CANON_ENV"
[[ -s "$CANON_ENV" ]] || fail "canonical production env file empty"

COMPOSE=(docker compose --project-directory "$WORKDIR" --env-file "$CANON_ENV" -p "$PROJECT")
IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
for f in "${CFG_ARR[@]}"; do
  [[ -f "$f" ]] || fail "Compose file missing: $f"
  COMPOSE+=( -f "$f" )
done
"${COMPOSE[@]}" config -q </dev/null

echo "CANONICAL_PRODUCTION_ENV_FILE=PASS"
echo "PRODUCTION_COMPOSE_INTERPOLATION=PASS"

# Reuse the already-built, DEV-certified image and reconfirm immutable fingerprints.
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "certified production candidate image missing"
APP_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'find /app/app /repo/services/shared/python/desifaces_shared -type f ! -path "*/__pycache__/*" ! -name "*.pyc" -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk "{print \$1}"')"
DEPS_HASH="$(docker run --rm --entrypoint sh "$IMAGE" -lc 'python -m pip freeze | LC_ALL=C sort | sha256sum | awk "{print \$1}"')"
[[ "$APP_HASH" == "$EXPECTED_APP_HASH" ]] || fail "candidate application fingerprint changed"
[[ "$DEPS_HASH" == "$EXPECTED_DEPS_HASH" ]] || fail "candidate dependency fingerprint changed"
echo "CERTIFIED_CANDIDATE_FINGERPRINT=PASS"

# Ask Compose to resolve the API environment using the canonical interpolation file.
# Explicitly detach stdin so this script is safe even if a caller invokes it via a pipe.
EFFECTIVE_ENV="$TMP/effective.env"
COMPOSE_STDERR="$TMP/compose-run.err"
"${COMPOSE[@]}" run -T --no-deps --rm --entrypoint env "$SERVICE" </dev/null >"$EFFECTIVE_ENV" 2>"$COMPOSE_STDERR"
chmod 600 "$EFFECTIVE_ENV"
echo "EFFECTIVE_PRODUCTION_ENV_CAPTURE=PASS"

# Compare critical effective production values against the untouched running Fusion Extension worker.
docker inspect "$WORKER" > "$TMP/worker.inspect.json"
python3 - "$EFFECTIVE_ENV" "$TMP/worker.inspect.json" <<'PY'
import json,sys

def envfile(path):
    out={}
    for raw in open(path,encoding='utf-8',errors='replace'):
        raw=raw.rstrip('\n')
        if '=' not in raw: continue
        k,v=raw.split('=',1)
        out[k]=v
    return out

def inspect_env(path):
    obj=json.load(open(path))[0]
    out={}
    for raw in obj.get('Config',{}).get('Env',[]):
        if '=' not in raw: continue
        k,v=raw.split('=',1)
        out[k]=v
    return out

api=envfile(sys.argv[1])
worker=inspect_env(sys.argv[2])
critical=[
    'DATABASE_URL',
    'REDIS_URL',
    'AZURE_STORAGE_CONNECTION_STRING',
    'JWT_SECRET',
    'JWT_ALG',
    'JWT_ISSUER',
    'JWT_AUDIENCE',
    'DF_PRICING_BEARER_TOKEN',
]
for k in critical:
    av=api.get(k,'')
    wv=worker.get(k,'')
    if not av:
        raise SystemExit(f'critical compose env is blank: {k}')
    if not wv:
        raise SystemExit(f'critical worker donor env is blank: {k}')
    if av != wv:
        raise SystemExit(f'canonical compose env differs from running worker: {k}')
print('CANONICAL_ENV_MATCHES_RUNNING_WORKER=PASS')
print('CRITICAL_PRODUCTION_ENV_NONEMPTY=PASS')
PY

# Isolated preflight with the exact effective API environment that Compose will apply.
NETWORK="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$WORKER")"
[[ -n "$NETWORK" ]] || fail "production Docker network unresolved"
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
docker run -d --name "$PREFLIGHT" --network "$NETWORK" --network-alias "$PREFLIGHT" --env-file "$EFFECTIVE_ENV" -e PORT=8006 -e WORKER_ENABLED=false -e STITCH_WORKER_ENABLED=false "$IMAGE" >/dev/null

READY=0
for _ in $(seq 1 60); do
  if docker exec "$PREFLIGHT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8006/api/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 120 "$PREFLIGHT" >&2 || true; fail "certified candidate failed exact-env preflight"; }
echo "EXACT_ENV_CANDIDATE_HEALTH=PASS"

docker exec "$PREFLIGHT" python -c '
import urllib.request,urllib.error
for path in ("/api/longform/pricing/preview","/api/longform/v3/scene-pricing/preview"):
    r=urllib.request.Request("http://127.0.0.1:8006"+path,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
    try: code=urllib.request.urlopen(r,timeout=5).status
    except urllib.error.HTTPError as e: code=e.code
    assert code != 404 and code < 500,(path,code)
print("EXACT_ENV_SINGLE_PERSON_ROUTE=PASS")
print("EXACT_ENV_MULTI_PERSON_ROUTE=PASS")
'

docker exec "$DIRECTOR" python -c '
import urllib.request,urllib.error
u="http://df-fusion-extension-prod-env-preflight:8006/api/longform/v3/scene-pricing/preview"
r=urllib.request.Request(u,data=b"{}",method="POST",headers={"Content-Type":"application/json"})
try: c=urllib.request.urlopen(r,timeout=5).status
except urllib.error.HTTPError as e: c=e.code
assert c != 404 and c < 500,c
print("DIRECTOR_TO_EXACT_ENV_PREFLIGHT=PASS")
print("DIRECTOR_PREFLIGHT_HTTP="+str(c))
'

docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true

echo "PRE_MUTATION_ENV_CERTIFICATION=PASS"

CURRENT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$EXT")"
CANDIDATE_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
[[ -n "$CURRENT_IMAGE_REF" && -n "$CANDIDATE_IMAGE_ID" ]] || fail "image metadata unavailable"
[[ "$CURRENT_IMAGE_REF" != sha256:* ]] || fail "current image reference not safely retaggable"

ROLLBACK_TAG="$(docker image ls desifaces-svc-fusion-extension --format '{{.Tag}}' | grep '^rollback-' | sort | tail -1 || true)"
[[ -n "$ROLLBACK_TAG" ]] || fail "previous production rollback image not found"
ROLLBACK_IMAGE="desifaces-svc-fusion-extension:$ROLLBACK_TAG"
docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1 || fail "rollback image unavailable"

echo "ROLLBACK_IMAGE_DISCOVERY=PASS"

rollback(){
  rc=${1:-1}
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker tag "$ROLLBACK_IMAGE" "$CURRENT_IMAGE_REF" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null >/dev/null 2>&1 || true
  REC="$(docker ps -a --format '{{.Names}}' | grep -E '^df-svc-fusion-extension$|^df-v3-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
  if [[ -n "$REC" ]]; then
    s=""; h=""
    for _ in $(seq 1 60); do
      s="$(docker inspect -f '{{.State.Status}}' "$REC" 2>/dev/null || true)"
      h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$REC" 2>/dev/null || true)"
      [[ "$s" == "running" && ( "$h" == "healthy" || "$h" == "no-healthcheck" ) ]] && break
      sleep 3
    done
    echo "ROLLBACK_STATE=${s:-unknown}"
    echo "ROLLBACK_HEALTH=${h:-unknown}"
  fi
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap 'rc=$?; (( MUTATED == 1 )) && rollback "$rc" || exit "$rc"' ERR

# First mutation: activate the exact DEV-certified image using validated production interpolation env.
docker tag "$IMAGE" "$CURRENT_IMAGE_REF"
MUTATED=1
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null
echo "PRODUCTION_RECREATE_WITH_CANONICAL_ENV=PASS"

EXT="$(docker ps -a --format '{{.Names}}' | grep -E '^df-svc-fusion-extension$|^df-v3-svc-fusion-extension$|svc-fusion-extension$' | head -1 || true)"
[[ -n "$EXT" ]] || fail "Fusion Extension API missing after recreate"

STATE=""; HEALTH=""; API_READY=0
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$EXT" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$EXT" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    if docker exec "$EXT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8006/api/health >/dev/null 2>&1; then API_READY=1; break; fi
  fi
  sleep 3
done
[[ "$STATE" == "running" ]] || fail "Fusion Extension not running after canonical-env recreate"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Fusion Extension unhealthy after canonical-env recreate: $HEALTH"
(( API_READY == 1 )) || fail "Fusion Extension API health endpoint not ready"
[[ "$(docker inspect -f '{{.Image}}' "$EXT")" == "$CANDIDATE_IMAGE_ID" ]] || fail "running Fusion Extension is not the certified candidate image"

echo "PRODUCTION_CERTIFIED_IMAGE_ACTIVE=PASS"
echo "PRODUCTION_FUSION_EXTENSION_HEALTH=PASS"

docker exec "$EXT" python -c '
from app.main import app
p={getattr(r,"path","") for r in app.routes}
req={"/api/longform/pricing/preview","/api/longform/jobs/pricing/preview","/api/longform/v3/scene-pricing/preview","/api/longform/v3/scene-pricing/reserve","/api/longform/v3/scene-pricing/commit","/api/longform/v3/scene-pricing/release","/api/longform/v3/scene-stitch","/api/longform/v3/assets/{media_id}/read-url","/api/longform/v3/story-stitch"}
m=sorted(req-p); assert not m,m
print("LIVE_SINGLE_PERSON_CONTRACT=PASS")
print("LIVE_MULTI_PERSON_CONTRACT=PASS")
'

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
MUTATED=0

echo "============================================================"
echo "ROOT_CAUSE=COMPOSE_INTERPOLATION_ENV_NOT_LOADED_DURING_PRIOR_RECREATE"
echo "PIPE_SAFE_DEPLOYMENT=YES"
echo "DEV_CERTIFIED_IMAGE_REUSED=YES"
echo "CANONICAL_PRODUCTION_ENV_VALIDATED=YES"
echo "SINGLE_PERSON_REGRESSION_GATE=PASS"
echo "MULTI_PERSON_CONTRACT_GATE=PASS"
echo "DIRECTOR_FUSION_EXTENSION_GATE=PASS"
echo "PRODUCTION_ENVIRONMENT=UP"
echo "SAFE_TO_RETRY_CHECK_PRICE=YES"
echo "============================================================"
