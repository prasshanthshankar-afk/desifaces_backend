#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  command -v runuser >/dev/null 2>&1 || { echo "FAIL: runuser unavailable" >&2; exit 1; }
  exec runuser -u azureuser -- env HOME=/home/azureuser PATH="$PATH" bash "$0" "$@"
fi

export HOME="${HOME:-/home/azureuser}"

EXPECTED_HOST="desifaces-gpu"
BASE_SHA="26b1dc59dde47cb79ff9ee08fd43d1dbaf0e73b5"
SOURCE_SHA="19f0102459618ddcc272433d437817b037ac28bf"
SOURCE_BRANCH="fix/v3-orphan-authoritative-status-20260914"
IMAGE="desifaces-svc-director:orphan-prod-${SOURCE_SHA:0:12}"
PREFLIGHT="df-director-orphan-prod-preflight"
TMP="$(mktemp -d /tmp/desifaces-director-orphan-prod.XXXXXX)"
WT="$TMP/source"
EFFECTIVE_ENV="$TMP/effective.env"
INSPECT="$TMP/director.inspect.json"
MUTATED=0

cleanup() {
  docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
  if [[ -n "${REPO:-}" && -d "${REPO:-}/.git" ]] && git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -Fq "worktree $WT"; then
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail(){ echo "FAIL: $*" >&2; exit 1; }
pass(){ echo "$1=PASS"; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "production hostname mismatch expected=$EXPECTED_HOST current=$(hostname -s)"
[[ "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" == "/var/lib/docker" ]] || fail "Docker root mismatch"
command -v docker >/dev/null 2>&1 || fail "docker missing"
command -v git >/dev/null 2>&1 || fail "git missing"

for candidate in /home/azureuser/workspace/desifaces-v3 /home/azureuser/workspace/desifaces_backend /home/azureuser/workspace/desifaces-backend; do
  if [[ -d "$candidate/.git" ]]; then REPO="$candidate"; break; fi
done
[[ -n "${REPO:-}" ]] || fail "production backend repository not found"

echo "============================================================"
echo " desifaces — PROD DIRECTOR ORPHAN STATUS RECOVERY"
echo " source_sha=$SOURCE_SHA"
echo " mutation_scope=DIRECTOR_API_ONLY"
echo " db_migration=NONE"
echo " fusion_worker_touch=NONE"
echo " face_audio_touch=NONE"
echo "============================================================"

# Exact source and change-scope gate.
git -C "$REPO" fetch --quiet origin "$SOURCE_BRANCH"
git -C "$REPO" cat-file -e "${SOURCE_SHA}^{commit}" 2>/dev/null || fail "source commit unavailable"
git -C "$REPO" cat-file -e "${BASE_SHA}^{commit}" 2>/dev/null || fail "base commit unavailable"
git -C "$REPO" worktree add --quiet --detach "$WT" "$SOURCE_SHA"
pass SOURCE_PIN_GATE

mapfile -t CHANGED < <(git -C "$WT" diff --name-only "$BASE_SHA" "$SOURCE_SHA" | sort)
EXPECTED=(
  "services/svc-director/app/app/fusion_execution_orphan_recovery.py"
  "services/svc-director/tests/test_fusion_orphan_authoritative_status.py"
)
[[ "${#CHANGED[@]}" -eq "${#EXPECTED[@]}" ]] || { printf 'changed files:\n%s\n' "${CHANGED[*]}" >&2; fail "source change scope widened"; }
for f in "${EXPECTED[@]}"; do printf '%s\n' "${CHANGED[@]}" | grep -Fxq "$f" || fail "expected changed file missing: $f"; done
pass SOURCE_DIFF_GATE

for untouched in \
  services/svc-director/app/app/fusion_execution_parent_pricing.py \
  services/svc-director/app/app/fusion_execution_performance.py \
  services/svc-director/app/app/fusion_execution_resilient.py \
  services/svc-director/app/app/fusion_execution.py \
  services/svc-director/app/app/main.py
 do
  git -C "$WT" diff --quiet "$BASE_SHA" "$SOURCE_SHA" -- "$untouched" || fail "unexpected V3 contract mutation: $untouched"
done
pass V3_CONTRACTS_UNCHANGED

# Build exact DEV-certified source on production host before any mutation.
docker build --pull=false -f "$WT/services/svc-director/app/Dockerfile.v3" -t "$IMAGE" "$WT" >"$TMP/build.log" 2>&1 || {
  tail -n 120 "$TMP/build.log" >&2 || true
  fail "Director candidate image build failed"
}
pass CANDIDATE_IMAGE_BUILD

DIRECTOR="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ -n "$DIRECTOR" ]] || fail "production Director API container not found"
[[ "$(docker inspect -f '{{.State.Status}}' "$DIRECTOR")" == "running" ]] || fail "production Director API is not currently running"
docker inspect "$DIRECTOR" > "$INSPECT"
pass PRODUCTION_DIRECTOR_DISCOVERY

WORKDIR="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$DIRECTOR" 2>/dev/null || true)"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$DIRECTOR" 2>/dev/null || true)"
SERVICE="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$DIRECTOR" 2>/dev/null || true)"
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$DIRECTOR" 2>/dev/null || true)"
[[ -n "$WORKDIR" && -d "$WORKDIR" ]] || fail "Compose working directory unavailable"
[[ -n "$CONFIG_FILES" && -n "$SERVICE" && -n "$PROJECT" ]] || fail "Compose ownership metadata incomplete"
CANON_ENV="$WORKDIR/infra/.env"
[[ -s "$CANON_ENV" ]] || fail "canonical production env missing or empty: $CANON_ENV"

COMPOSE=(docker compose --project-directory "$WORKDIR" --env-file "$CANON_ENV" -p "$PROJECT")
IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
for f in "${CFG_ARR[@]}"; do [[ -f "$f" ]] || fail "Compose file missing: $f"; COMPOSE+=( -f "$f" ); done
"${COMPOSE[@]}" config -q </dev/null
pass PRODUCTION_COMPOSE_CONFIG

# Capture exactly what Compose would apply and verify it against the currently healthy Director.
"${COMPOSE[@]}" run -T --no-deps --rm --entrypoint env "$SERVICE" </dev/null >"$EFFECTIVE_ENV" 2>"$TMP/compose-run.err"
chmod 600 "$EFFECTIVE_ENV"
python3 - "$EFFECTIVE_ENV" "$INSPECT" <<'PY'
import json,sys

def envfile(path):
    out={}
    for raw in open(path,encoding='utf-8',errors='replace'):
        raw=raw.rstrip('\n')
        if '=' in raw:
            k,v=raw.split('=',1); out[k]=v
    return out

def inspect_env(path):
    obj=json.load(open(path))[0]
    out={}
    for raw in obj.get('Config',{}).get('Env',[]):
        if '=' in raw:
            k,v=raw.split('=',1); out[k]=v
    return out

new=envfile(sys.argv[1]); old=inspect_env(sys.argv[2])
required=['DATABASE_URL','JWT_SECRET']
for k in required:
    if not new.get(k): raise SystemExit(f'compose env blank: {k}')
    if not old.get(k): raise SystemExit(f'running Director env blank: {k}')
    if new[k] != old[k]: raise SystemExit(f'compose env differs from running Director: {k}')
for k in ['JWT_ALG','JWT_ISSUER','JWT_AUDIENCE','DF_FACE_BASE_URL','DF_AUDIO_BASE_URL','DF_FUSION_BASE_URL','DF_FUSION_EXTENSION_BASE_URL']:
    ov=old.get(k,''); nv=new.get(k,'')
    if ov and nv and ov != nv: raise SystemExit(f'compose env differs from running Director: {k}')
print('CANONICAL_ENV_MATCHES_RUNNING_DIRECTOR=PASS')
print('CRITICAL_DIRECTOR_ENV_NONEMPTY=PASS')
PY
pass PRODUCTION_ENVIRONMENT_GATE

NETWORK="$(python3 - "$INSPECT" <<'PY'
import json,sys
nets=list(json.load(open(sys.argv[1]))[0].get('NetworkSettings',{}).get('Networks',{}))
if not nets: raise SystemExit(2)
print(nets[0])
PY
)"
[[ -n "$NETWORK" ]] || fail "production Director network unresolved"

# Re-run the DEV-certified safety semantics on the exact production candidate image.
docker run --rm -i --env-file "$EFFECTIVE_ENV" -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false --entrypoint python "$IMAGE" <<'PY'
import asyncio
from app.fusion_execution_orphan_recovery import _authoritative_child_status

class Fake:
    def __init__(self, light, full=None, full_raises=False):
        self.light=dict(light); self.full=dict(full or {}); self.full_raises=full_raises; self.full_calls=0
    async def status(self, *, headers, job_id): return dict(self.light)
    async def status_full(self, *, headers, job_id):
        self.full_calls += 1
        if self.full_raises: raise RuntimeError('full unavailable')
        return dict(self.full)

async def main():
    f=Fake({'status':'queued','artifacts':[]},{'status':'succeeded','artifacts':[{'kind':'video','url':'https://example.invalid/completed.mp4?sig=fresh'}]})
    s,u=await _authoritative_child_status(f,headers={},job_id='completed',persisted_state='queued')
    assert s=='succeeded' and u and f.full_calls==1
    print('STALE_QUEUED_FULL_SUCCESS_REUSE=PASS')

    f=Fake({'status':'running'},{'status':'running'})
    s,u=await _authoritative_child_status(f,headers={},job_id='running',persisted_state='queued')
    assert s=='running' and not u and f.full_calls==1
    print('TRUE_RUNNING_FAIL_CLOSED=PASS')

    f=Fake({'status':'failed'})
    s,u=await _authoritative_child_status(f,headers={},job_id='failed',persisted_state='queued')
    assert s=='failed' and not u
    print('TERMINAL_FAILURE_RETRY_SEMANTICS=PASS')

    f=Fake({'status':'succeeded','video_url':'https://example.invalid/light.mp4?sig=fresh'})
    s,u=await _authoritative_child_status(f,headers={},job_id='success',persisted_state='queued')
    assert s=='succeeded' and u and f.full_calls==0
    print('HEALTHY_LIGHT_SUCCESS_UNCHANGED=PASS')

    f=Fake({'status':'queued'},full_raises=True)
    s,u=await _authoritative_child_status(f,headers={},job_id='unknown',persisted_state='queued')
    assert s=='queued' and not u and f.full_calls==1
    print('FULL_STATUS_FAILURE_FAIL_CLOSED=PASS')

asyncio.run(main())
PY
pass PRODUCTION_PREMUTATION_SAFETY_TESTS

# Isolated candidate: same network/env, no host port, checkpoint setup disabled.
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
docker run -d --name "$PREFLIGHT" --network "$NETWORK" --env-file "$EFFECTIVE_ENV" -e PORT=8011 -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false "$IMAGE" >/dev/null
READY=0
for _ in $(seq 1 45); do
  if docker exec "$PREFLIGHT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 160 "$PREFLIGHT" >&2 || true; fail "isolated production candidate failed health"; }
pass EXACT_ENV_CANDIDATE_HEALTH

docker exec "$PREFLIGHT" python -c 'from app.main import app; p={getattr(r,"path","") for r in app.routes}; assert "/api/health" in p; from app.fusion_execution_orphan_recovery import _authoritative_child_status; print("CANDIDATE_FULL_APP_IMPORT=PASS")'
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
pass PRE_MUTATION_GATE

CURRENT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$DIRECTOR")"
CURRENT_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$DIRECTOR")"
CANDIDATE_IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
[[ -n "$CURRENT_IMAGE_REF" && -n "$CURRENT_IMAGE_ID" && -n "$CANDIDATE_IMAGE_ID" ]] || fail "image metadata unavailable"
[[ "$CURRENT_IMAGE_REF" != sha256:* ]] || fail "current Director image reference is not safely retaggable"
ROLLBACK_IMAGE="desifaces-svc-director:rollback-orphan-$(date -u +%Y%m%dT%H%M%SZ)"
docker tag "$CURRENT_IMAGE_ID" "$ROLLBACK_IMAGE"
pass ROLLBACK_IMAGE_CAPTURE

rollback(){
  rc=${1:-1}
  trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker tag "$ROLLBACK_IMAGE" "$CURRENT_IMAGE_REF" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null >/dev/null 2>&1 || true
  REC="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
  if [[ -n "$REC" ]]; then
    for _ in $(seq 1 45); do
      st="$(docker inspect -f '{{.State.Status}}' "$REC" 2>/dev/null || true)"
      if [[ "$st" == "running" ]] && docker exec "$REC" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then break; fi
      sleep 2
    done
    echo "ROLLBACK_STATE=${st:-unknown}"
  fi
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}
trap 'rc=$?; (( MUTATED == 1 )) && rollback "$rc" || exit "$rc"' ERR

# First and only production mutation: Director API image swap/recreate.
docker tag "$IMAGE" "$CURRENT_IMAGE_REF"
MUTATED=1
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null
pass PRODUCTION_DIRECTOR_RECREATE

DIRECTOR="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ -n "$DIRECTOR" ]] || fail "Director container missing after recreate"
LIVE_READY=0
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$DIRECTOR" 2>/dev/null || true)"
  if [[ "$STATE" == "running" ]] && docker exec "$DIRECTOR" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then LIVE_READY=1; break; fi
  sleep 2
done
(( LIVE_READY == 1 )) || fail "Director API failed post-deploy health"
[[ "$(docker inspect -f '{{.Image}}' "$DIRECTOR")" == "$CANDIDATE_IMAGE_ID" ]] || fail "running Director is not candidate image"
pass PRODUCTION_DIRECTOR_HEALTH
pass PRODUCTION_CANDIDATE_IMAGE_ACTIVE

# Live code assertion, no DB write and no provider call.
docker exec "$DIRECTOR" python -c 'import inspect; from app.fusion_execution_orphan_recovery import _authoritative_child_status; s=inspect.getsource(_authoritative_child_status); assert "status_full" in s and "needs_full" in s; print("LIVE_AUTHORITATIVE_STATUS_FIX=PASS")'

# Existing service connectivity remains available after the single-container recreate.
docker exec "$DIRECTOR" python -c '
import urllib.request,urllib.error
from app.config import settings
for name,base,path in [
 ("fusion",settings.DF_FUSION_BASE_URL,"/api/health"),
 ("fusion_extension",settings.DF_FUSION_EXTENSION_BASE_URL,"/api/health"),
 ("face",settings.DF_FACE_BASE_URL,"/api/health"),
 ("audio",settings.DF_AUDIO_BASE_URL,"/api/health"),
]:
 u=str(base).rstrip("/")+path
 try: c=urllib.request.urlopen(u,timeout=5).status
 except urllib.error.HTTPError as e: c=e.code
 assert c < 500,(name,c)
 print(f"LIVE_{name.upper()}_CONNECTIVITY=PASS http={c}")
'

trap - ERR
MUTATED=0

echo "============================================================"
echo "DEV_CERTIFIED_SOURCE=$SOURCE_SHA"
echo "PRODUCTION_CHANGE_SCOPE=DIRECTOR_API_ONLY"
echo "DB_MIGRATION=NONE"
echo "NEW_PROVIDER_JOB_CREATED=NO"
echo "FUSION_WORKER_TOUCH=NONE"
echo "FACE_AUDIO_TOUCH=NONE"
echo "ROLLBACK_IMAGE=$ROLLBACK_IMAGE"
echo "PRODUCTION_ORPHAN_AUTHORITATIVE_STATUS_FIX=PASS"
echo "SAFE_TO_RETRY_SAME_STORY_CHECK_PRICE=YES"
echo "============================================================"
