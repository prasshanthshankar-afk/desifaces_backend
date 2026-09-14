#!/usr/bin/env bash
set -Eeuo pipefail

# Repo-free, SSH-native production promotion of the DEV-certified orphan-child
# authoritative-status fix.  The candidate is derived from the exact currently
# running Director image and replaces one Python file only.

EXPECTED_HOST="desifaces-gpu"
EXPECTED_DIRECTOR="df-v3-svc-director"
EXPECTED_BASE_IMAGE_REF="desifaces-v3-svc-director-production"
EXPECTED_BASE_IMAGE_ID="sha256:2aa183a8631300eacfa855683a80f3b240a1fc05f519845302f70830662435d0"
EXPECTED_OLD_FILE_SHA256="9b9fd71e684b0a8277736439591f9699f65ad627c2da0bc809187f1f3216a63b"
SOURCE_SHA="19f0102459618ddcc272433d437817b037ac28bf"
EXPECTED_PATCH_BLOB_SHA="e373dc54d3482f8ec08ba449dcd0167d6af93b12"
PATCH_URL="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/svc-director/app/app/fusion_execution_orphan_recovery.py"
WORKDIR="/home/azureuser/workspace/desifaces"
CANON_ENV="$WORKDIR/infra/.env"
SERVICE="svc-director"
PROJECT="desifaces"
NETWORK="df-net"
TMP="$(mktemp -d /tmp/desifaces-orphan-prod-ssh.XXXXXX)"
PATCH="$TMP/fusion_execution_orphan_recovery.py"
ENVFILE="$TMP/current-director.env"
COMPOSE_JSON="$TMP/compose.json"
CANDIDATE="desifaces-v3-svc-director:orphan-prod-${SOURCE_SHA:0:12}"
PREFLIGHT="df-director-orphan-prod-preflight"
ROLLBACK_TAG="desifaces-v3-svc-director:rollback-orphan-$(date -u +%Y%m%dT%H%M%SZ)"
MUTATED=0

cleanup() {
  docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
  rm -rf "$TMP" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; return 1; }
pass() { echo "$1=PASS"; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "production host guard"
command -v docker >/dev/null 2>&1 || fail "docker missing"
command -v curl >/dev/null 2>&1 || fail "curl missing"
command -v git >/dev/null 2>&1 || fail "git missing"
command -v python3 >/dev/null 2>&1 || fail "python3 missing"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"

DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ "$DIRECTOR" == "$EXPECTED_DIRECTOR" ]] || fail "unexpected Director container: ${DIRECTOR:-missing}"
IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$DIRECTOR")"
IMAGE_ID="$(docker inspect -f '{{.Image}}' "$DIRECTOR")"
[[ "$IMAGE_REF" == "$EXPECTED_BASE_IMAGE_REF" ]] || fail "Director image ref changed: $IMAGE_REF"
[[ "$IMAGE_ID" == "$EXPECTED_BASE_IMAGE_ID" ]] || fail "Director image id changed: $IMAGE_ID"
[[ "$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$DIRECTOR")" == "$NETWORK" ]] || fail "Director network changed"
CURRENT_FILE_SHA="$(docker exec "$DIRECTOR" sha256sum /app/app/fusion_execution_orphan_recovery.py | awk '{print $1}')"
[[ "$CURRENT_FILE_SHA" == "$EXPECTED_OLD_FILE_SHA256" ]] || fail "production target file changed since read-only baseline"
pass PRODUCTION_BASELINE_PIN

echo "============================================================"
echo " desifaces — DIRECT SSH PROD DIRECTOR RECOVERY"
echo " source_sha=$SOURCE_SHA"
echo " mutation_scope=ONE_DIRECTOR_FILE"
echo " db_migration=NONE"
echo " face_audio_touch=NONE"
echo " fusion_api_worker_touch=NONE"
echo "============================================================"

# Download the exact DEV-certified file and verify its Git blob identity.
curl -fsSL --connect-timeout 5 --max-time 30 "$PATCH_URL" -o "$PATCH"
[[ "$(git hash-object "$PATCH")" == "$EXPECTED_PATCH_BLOB_SHA" ]] || fail "downloaded patch does not match certified Git blob"
PATCH_SHA256="$(sha256sum "$PATCH" | awk '{print $1}')"
grep -Fq 'async def _authoritative_child_status(' "$PATCH" || fail "certified helper missing"
grep -Fq 'status_full = getattr(fusion_client, "status_full", None)' "$PATCH" || fail "authoritative full-status fallback missing"
pass CERTIFIED_PATCH_IDENTITY

# Capture the exact current runtime environment without printing secrets.
docker inspect "$DIRECTOR" > "$TMP/director.inspect.json"
python3 - "$TMP/director.inspect.json" "$ENVFILE" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1]))[0]
with open(sys.argv[2],"w") as out:
    for raw in obj.get("Config",{}).get("Env",[]):
        if "=" not in raw:
            continue
        k,v=raw.split("=",1)
        if "\n" in v or "\r" in v:
            raise SystemExit(f"unsupported newline env: {k}")
        out.write(f"{k}={v}\n")
PY
chmod 600 "$ENVFILE"
pass CURRENT_RUNTIME_ENV_CAPTURE

# Validate Compose ownership and canonical interpolation before any mutation.
WORKDIR_LABEL="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$DIRECTOR" 2>/dev/null || true)"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$DIRECTOR" 2>/dev/null || true)"
SERVICE_LABEL="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$DIRECTOR" 2>/dev/null || true)"
PROJECT_LABEL="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$DIRECTOR" 2>/dev/null || true)"
[[ "$WORKDIR_LABEL" == "$WORKDIR" ]] || fail "Compose working directory changed"
[[ "$SERVICE_LABEL" == "$SERVICE" && "$PROJECT_LABEL" == "$PROJECT" ]] || fail "Compose ownership changed"
[[ -f "$CANON_ENV" && -s "$CANON_ENV" ]] || fail "canonical production env unavailable"

COMPOSE=(docker compose --project-directory "$WORKDIR" --env-file "$CANON_ENV" -p "$PROJECT")
IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
for f in "${CFG_ARR[@]}"; do
  [[ -f "$f" ]] || fail "Compose file missing: $f"
  COMPOSE+=( -f "$f" )
done
"${COMPOSE[@]}" config -q </dev/null
"${COMPOSE[@]}" config --format json > "$COMPOSE_JSON"
python3 - "$COMPOSE_JSON" "$SERVICE" "$IMAGE_REF" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
svc=(cfg.get("services") or {}).get(sys.argv[2]) or {}
image=str(svc.get("image") or "")
if image != sys.argv[3]:
    raise SystemExit(f"compose service image mismatch: {image}")
print("COMPOSE_IMAGE_OWNERSHIP=PASS")
PY
pass CANONICAL_COMPOSE_CONFIG

# Validate that Compose would preserve the important Director runtime environment.
"${COMPOSE[@]}" run -T --no-deps --rm --entrypoint env "$SERVICE" </dev/null > "$TMP/compose-effective.env"
python3 - "$ENVFILE" "$TMP/compose-effective.env" <<'PY'
import sys
def read(path):
    out={}
    for raw in open(path,encoding="utf-8",errors="replace"):
        raw=raw.rstrip("\n")
        if "=" in raw:
            k,v=raw.split("=",1); out[k]=v
    return out
live,eff=read(sys.argv[1]),read(sys.argv[2])
keys={k for k in live if k.startswith(("DF_","JWT_","OPENAI_","AZURE_"))}
keys.update({"DATABASE_URL"})
missing=[]; mismatch=[]
for k in sorted(keys):
    if k not in eff: missing.append(k)
    elif live.get(k) != eff.get(k): mismatch.append(k)
if missing or mismatch:
    raise SystemExit("compose runtime env mismatch; missing="+",".join(missing)+" mismatch="+",".join(mismatch))
print("COMPOSE_RUNTIME_ENV_MATCH=PASS")
PY

# Preserve rollback image before building or retagging anything.
docker tag "$IMAGE_ID" "$ROLLBACK_TAG"
docker image inspect "$ROLLBACK_TAG" >/dev/null
pass ROLLBACK_IMAGE_CAPTURE

# Build candidate FROM the exact running production image; replace one file only.
cat > "$TMP/Dockerfile" <<EOF
FROM $ROLLBACK_TAG
COPY fusion_execution_orphan_recovery.py /app/app/fusion_execution_orphan_recovery.py
EOF
cp "$PATCH" "$TMP/fusion_execution_orphan_recovery.py"
docker build --pull=false -t "$CANDIDATE" "$TMP" > "$TMP/build.log" 2>&1 || { tail -n 120 "$TMP/build.log" >&2 || true; fail "candidate build failed"; }
CANDIDATE_ID="$(docker image inspect -f '{{.Id}}' "$CANDIDATE")"
[[ -n "$CANDIDATE_ID" ]] || fail "candidate image id missing"
CANDIDATE_FILE_SHA="$(docker run --rm --entrypoint sh "$CANDIDATE" -lc 'sha256sum /app/app/fusion_execution_orphan_recovery.py' | awk '{print $1}')"
[[ "$CANDIDATE_FILE_SHA" == "$PATCH_SHA256" ]] || fail "candidate target file fingerprint mismatch"
pass ONE_FILE_CANDIDATE_BUILD

# Exact safety contract used in DEV certification.
docker run --rm -i --env-file "$ENVFILE" -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false --entrypoint python "$CANDIDATE" <<'PY'
import asyncio
from app.fusion_execution_orphan_recovery import _authoritative_child_status

class Fake:
    def __init__(self, light, full=None, full_raises=False):
        self.light=dict(light); self.full=dict(full or {}); self.full_raises=full_raises
        self.light_calls=0; self.full_calls=0
    async def status(self, *, headers, job_id):
        self.light_calls += 1; return dict(self.light)
    async def status_full(self, *, headers, job_id):
        self.full_calls += 1
        if self.full_raises: raise RuntimeError("full unavailable")
        return dict(self.full)

async def main():
    f=Fake({"status":"queued","artifacts":[]},{"status":"succeeded","artifacts":[{"kind":"video","url":"https://example.invalid/completed.mp4?sig=fresh"}]})
    state,url=await _authoritative_child_status(f,headers={},job_id="completed",persisted_state="queued")
    assert state=="succeeded" and url and f.light_calls==1 and f.full_calls==1
    print("STALE_QUEUED_FULL_SUCCESS_REUSE=PASS")

    f=Fake({"status":"running"},{"status":"running"})
    state,url=await _authoritative_child_status(f,headers={},job_id="running",persisted_state="queued")
    assert state=="running" and not url and f.full_calls==1
    print("TRUE_RUNNING_FAIL_CLOSED=PASS")

    f=Fake({"status":"failed"})
    state,url=await _authoritative_child_status(f,headers={},job_id="failed",persisted_state="queued")
    assert state=="failed" and not url and f.full_calls==0
    print("TERMINAL_FAILURE_RETRY_SEMANTICS=PASS")

    f=Fake({"status":"succeeded","video_url":"https://example.invalid/light.mp4?sig=fresh"})
    state,url=await _authoritative_child_status(f,headers={},job_id="healthy",persisted_state="queued")
    assert state=="succeeded" and url and f.full_calls==0
    print("HEALTHY_LIGHT_SUCCESS_UNCHANGED=PASS")

    f=Fake({"status":"queued"},full_raises=True)
    state,url=await _authoritative_child_status(f,headers={},job_id="unknown",persisted_state="queued")
    assert state=="queued" and not url and f.full_calls==1
    print("FULL_STATUS_FAILURE_FAIL_CLOSED=PASS")

asyncio.run(main())
PY
pass FOCUSED_SAFETY_TESTS

# Route surface must remain byte-for-byte identical between rollback and candidate.
for img in "$ROLLBACK_TAG" "$CANDIDATE"; do
  out="$TMP/routes.$(echo "$img" | tr '/:' '__')"
  docker run --rm --env-file "$ENVFILE" -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false --entrypoint python "$img" -c \
    'from app.main import app; import json; print(json.dumps(sorted({(getattr(r,"path","") or "")+"|"+",".join(sorted(getattr(r,"methods",set()) or set())) for r in app.routes})))' > "$out"
done
BASE_ROUTES="$TMP/routes.$(echo "$ROLLBACK_TAG" | tr '/:' '__')"
CAND_ROUTES="$TMP/routes.$(echo "$CANDIDATE" | tr '/:' '__')"
cmp -s "$BASE_ROUTES" "$CAND_ROUTES" || fail "Director route surface changed"
pass DIRECTOR_ROUTE_SURFACE_UNCHANGED

# Isolated production-network preflight, with no host port and no production name/alias.
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
docker run -d --name "$PREFLIGHT" --network "$NETWORK" --env-file "$ENVFILE" -e PORT=8011 -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false "$CANDIDATE" >/dev/null
READY=0
for _ in $(seq 1 45); do
  if docker exec "$PREFLIGHT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 160 "$PREFLIGHT" >&2 || true; fail "isolated candidate health failed"; }
pass ISOLATED_PRODUCTION_ENV_HEALTH

docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
pass PRE_MUTATION_CERTIFICATION

rollback() {
  rc=${1:-1}
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker tag "$ROLLBACK_TAG" "$IMAGE_REF" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null >/dev/null 2>&1 || true
  REC="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
  if [[ -n "$REC" ]]; then
    for _ in $(seq 1 45); do
      s="$(docker inspect -f '{{.State.Status}}' "$REC" 2>/dev/null || true)"
      h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$REC" 2>/dev/null || true)"
      if [[ "$s" == "running" && ( "$h" == "healthy" || "$h" == "no-healthcheck" ) ]]; then break; fi
      sleep 2
    done
    echo "ROLLBACK_STATE=${s:-unknown}"
    echo "ROLLBACK_HEALTH=${h:-unknown}"
  fi
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}

# First runtime mutation: point the Compose image tag to the preflighted candidate.
docker tag "$CANDIDATE" "$IMAGE_REF"
MUTATED=1
trap 'rc=$?; (( MUTATED == 1 )) && rollback "$rc" || exit "$rc"' ERR
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null
pass DIRECTOR_ONLY_RECREATE

NEW_DIRECTOR="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ "$NEW_DIRECTOR" == "$EXPECTED_DIRECTOR" ]] || fail "Director container missing after recreate"
STATE=""; HEALTH=""; API_READY=0
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$NEW_DIRECTOR" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$NEW_DIRECTOR" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    if docker exec "$NEW_DIRECTOR" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then API_READY=1; break; fi
  fi
  sleep 2
done
[[ "$STATE" == "running" ]] || fail "Director not running after promotion"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Director unhealthy after promotion: $HEALTH"
(( API_READY == 1 )) || fail "Director health endpoint not ready"
[[ "$(docker inspect -f '{{.Image}}' "$NEW_DIRECTOR")" == "$CANDIDATE_ID" ]] || fail "running Director image is not candidate"
LIVE_FILE_SHA="$(docker exec "$NEW_DIRECTOR" sha256sum /app/app/fusion_execution_orphan_recovery.py | awk '{print $1}')"
[[ "$LIVE_FILE_SHA" == "$PATCH_SHA256" ]] || fail "live target file fingerprint mismatch"
pass LIVE_CANDIDATE_ACTIVE

# Verify full app import and both downstream service health paths from the live Director.
docker exec "$NEW_DIRECTOR" python - <<'PY'
import urllib.request
from app.main import app
from app.config import settings
assert any(getattr(r,"path","")=="/api/health" for r in app.routes)
for name,base in (
    ("fusion",str(settings.DF_FUSION_BASE_URL).rstrip("/")),
    ("fusion_extension",str(settings.DF_FUSION_EXTENSION_BASE_URL).rstrip("/")),
):
    with urllib.request.urlopen(base+"/api/health",timeout=5) as r:
        assert 200 <= r.status < 300,(name,r.status)
    print(name.upper()+"_CONNECTIVITY=PASS")
print("LIVE_DIRECTOR_APP_IMPORT=PASS")
PY

trap - ERR
MUTATED=0

echo "============================================================"
echo "DEV_CERTIFIED_SOURCE=$SOURCE_SHA"
echo "PATCHED_PRODUCTION_COMPONENT=DIRECTOR_API_ONLY"
echo "PATCHED_PRODUCTION_FILE=services/svc-director/app/app/fusion_execution_orphan_recovery.py"
echo "DATABASE_MIGRATION=NONE"
echo "FACE_AUDIO_TOUCH=NONE"
echo "FUSION_API_WORKER_TOUCH=NONE"
echo "ROLLBACK_IMAGE=$ROLLBACK_TAG"
echo "STALE_QUEUED_CHILD_RECOVERY=PASS"
echo "TRUE_RUNNING_CHILD_DUPLICATE_GUARD=PASS"
echo "PRODUCTION_DIRECTOR_HEALTH=PASS"
echo "SAFE_TO_RETRY_SAME_STORY_CHECK_PRICE=YES"
echo "PRODUCTION_PROMOTION=PASS"
echo "============================================================"
