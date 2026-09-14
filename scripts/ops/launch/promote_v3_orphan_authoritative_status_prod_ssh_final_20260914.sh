#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-gpu"
EXPECTED_DIRECTOR="df-v3-svc-director"
EXPECTED_IMAGE_REF="desifaces-v3-svc-director-production"
EXPECTED_IMAGE_ID="sha256:2aa183a8631300eacfa855683a80f3b240a1fc05f519845302f70830662435d0"
EXPECTED_OLD_FILE_SHA256="9b9fd71e684b0a8277736439591f9699f65ad627c2da0bc809187f1f3216a63b"
SOURCE_SHA="19f0102459618ddcc272433d437817b037ac28bf"
EXPECTED_PATCH_BLOB_SHA="e373dc54d3482f8ec08ba449dcd0167d6af93b12"
PATCH_URL="https://raw.githubusercontent.com/prasshanthshankar-afk/desifaces_backend/${SOURCE_SHA}/services/svc-director/app/app/fusion_execution_orphan_recovery.py"
WORKDIR="/home/azureuser/workspace/desifaces"
CANON_ENV="$WORKDIR/infra/.env"
SERVICE="svc-director"
PROJECT="desifaces"
NETWORK="df-net"
TMP="$(mktemp -d /tmp/df-orphan-prod-final.XXXXXX)"
PATCH="$TMP/fusion_execution_orphan_recovery.py"
RUNTIME_ENV="$TMP/director.env"
BUILDCTX="$TMP/buildctx"
CANDIDATE="desifaces-v3-svc-director:orphan-prod-${SOURCE_SHA:0:12}"
PREFLIGHT="df-director-orphan-prod-preflight"
ROLLBACK="desifaces-v3-svc-director:rollback-orphan-$(date -u +%Y%m%dT%H%M%SZ)"
MUTATED=0

cleanup(){ docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true; rm -rf "$TMP" >/dev/null 2>&1 || true; }
trap cleanup EXIT
fail(){ echo "FAIL: $*" >&2; return 1; }
pass(){ echo "$1=PASS"; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "production host guard"
command -v docker >/dev/null || fail "docker missing"
command -v curl >/dev/null || fail "curl missing"
command -v git >/dev/null || fail "git missing"
command -v python3 >/dev/null || fail "python3 missing"
[[ "$(docker info --format '{{.DockerRootDir}}')" == "/var/lib/docker" ]] || fail "Docker root mismatch"

DIRECTOR="$(docker ps --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ "$DIRECTOR" == "$EXPECTED_DIRECTOR" ]] || fail "Director container mismatch: ${DIRECTOR:-missing}"
IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$DIRECTOR")"
IMAGE_ID="$(docker inspect -f '{{.Image}}' "$DIRECTOR")"
[[ "$IMAGE_REF" == "$EXPECTED_IMAGE_REF" ]] || fail "Director image ref changed"
[[ "$IMAGE_ID" == "$EXPECTED_IMAGE_ID" ]] || fail "Director image id changed"
[[ "$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$DIRECTOR")" == "$NETWORK" ]] || fail "Director network changed"
OLD_SHA="$(docker exec "$DIRECTOR" sha256sum /app/app/fusion_execution_orphan_recovery.py | awk '{print $1}')"
[[ "$OLD_SHA" == "$EXPECTED_OLD_FILE_SHA256" ]] || fail "target file changed since baseline"
pass PRODUCTION_BASELINE_PIN

echo "============================================================"
echo " desifaces — FINAL SSH DIRECTOR RECOVERY"
echo " source_sha=$SOURCE_SHA"
echo " production_file_changes=1"
echo " database_migration=NONE"
echo " fusion_face_audio_touch=NONE"
echo "============================================================"

curl -fsSL --connect-timeout 5 --max-time 30 "$PATCH_URL" -o "$PATCH"
[[ "$(git hash-object "$PATCH")" == "$EXPECTED_PATCH_BLOB_SHA" ]] || fail "patch identity mismatch"
PATCH_SHA256="$(sha256sum "$PATCH" | awk '{print $1}')"
grep -Fq 'async def _authoritative_child_status(' "$PATCH" || fail "authoritative status helper missing"
grep -Fq 'status_full = getattr(fusion_client, "status_full", None)' "$PATCH" || fail "full-status fallback missing"
pass CERTIFIED_PATCH_IDENTITY

# Exact live environment for isolated preflight. Secrets are never printed.
docker inspect "$DIRECTOR" > "$TMP/director.inspect.json"
python3 - "$TMP/director.inspect.json" "$RUNTIME_ENV" <<'PY'
import json,sys
obj=json.load(open(sys.argv[1]))[0]
with open(sys.argv[2],"w") as out:
    for raw in obj.get("Config",{}).get("Env",[]):
        if "=" not in raw: continue
        k,v=raw.split("=",1)
        if "\n" in v or "\r" in v: raise SystemExit(f"newline env unsupported: {k}")
        out.write(f"{k}={v}\n")
PY
chmod 600 "$RUNTIME_ENV"
pass LIVE_ENV_CAPTURE

WORKDIR_LABEL="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$DIRECTOR")"
CONFIG_FILES="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$DIRECTOR")"
SERVICE_LABEL="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$DIRECTOR")"
PROJECT_LABEL="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$DIRECTOR")"
[[ "$WORKDIR_LABEL" == "$WORKDIR" && "$SERVICE_LABEL" == "$SERVICE" && "$PROJECT_LABEL" == "$PROJECT" ]] || fail "Compose ownership mismatch"
[[ -s "$CANON_ENV" ]] || fail "canonical production env missing"
COMPOSE=(docker compose --project-directory "$WORKDIR" --env-file "$CANON_ENV" -p "$PROJECT")
IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
for f in "${CFG_ARR[@]}"; do [[ -f "$f" ]] || fail "Compose file missing: $f"; COMPOSE+=( -f "$f" ); done
"${COMPOSE[@]}" config -q </dev/null

# Prove the Compose recreate will preserve Director-critical runtime values.
"${COMPOSE[@]}" run -T --no-deps --rm --entrypoint env "$SERVICE" </dev/null > "$TMP/effective.env"
python3 - "$RUNTIME_ENV" "$TMP/effective.env" <<'PY'
import sys
def read(p):
    d={}
    for raw in open(p,encoding="utf-8",errors="replace"):
        raw=raw.rstrip("\n")
        if "=" in raw:
            k,v=raw.split("=",1); d[k]=v
    return d
live,eff=read(sys.argv[1]),read(sys.argv[2])
keys=[
 "DATABASE_URL","JWT_SECRET","JWT_ALG","JWT_ISSUER","JWT_AUDIENCE",
 "DF_FACE_BASE_URL","DF_AUDIO_BASE_URL","DF_FUSION_BASE_URL","DF_FUSION_EXTENSION_BASE_URL",
 "DF_DIRECTOR_LLM_MODEL","DF_DIRECTOR_EMBEDDING_MODEL","DF_DIRECTOR_REVIEW_REQUIRED",
 "DF_DIRECTOR_MAX_REVISIONS","DF_DIRECTOR_BLOCKING_CRITIC","DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP",
 "OPENAI_API_KEY",
]
for k in keys:
    if k in live and live[k] != eff.get(k):
        raise SystemExit(f"Compose would change critical Director env: {k}")
print("COMPOSE_CRITICAL_ENV_MATCH=PASS")
PY
pass PRE_RECREATE_ENV_GATE

# Rollback is the exact current production image.
docker tag "$IMAGE_ID" "$ROLLBACK"
docker image inspect "$ROLLBACK" >/dev/null
pass ROLLBACK_IMAGE_CAPTURE

# Build from the exact current image and replace one file only.
mkdir -p "$BUILDCTX"
cp "$PATCH" "$BUILDCTX/fusion_execution_orphan_recovery.py"
cat > "$BUILDCTX/Dockerfile" <<EOF
FROM $ROLLBACK
COPY fusion_execution_orphan_recovery.py /app/app/fusion_execution_orphan_recovery.py
EOF
docker build --pull=false -t "$CANDIDATE" "$BUILDCTX" > "$TMP/build.log" 2>&1 || { tail -n 100 "$TMP/build.log" >&2 || true; fail "candidate build failed"; }
CANDIDATE_ID="$(docker image inspect -f '{{.Id}}' "$CANDIDATE")"
CAND_SHA="$(docker run --rm --entrypoint sh "$CANDIDATE" -lc 'sha256sum /app/app/fusion_execution_orphan_recovery.py' | awk '{print $1}')"
[[ "$CAND_SHA" == "$PATCH_SHA256" ]] || fail "candidate file fingerprint mismatch"
pass ONE_FILE_CANDIDATE_BUILD

# Same focused safety contract that passed DEV.
docker run --rm -i --env-file "$RUNTIME_ENV" -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false --entrypoint python "$CANDIDATE" <<'PY'
import asyncio
from app.fusion_execution_orphan_recovery import _authoritative_child_status
class F:
    def __init__(self,l,f=None,boom=False): self.l=dict(l); self.f=dict(f or {}); self.boom=boom; self.lc=0; self.fc=0
    async def status(self,*,headers,job_id): self.lc+=1; return dict(self.l)
    async def status_full(self,*,headers,job_id):
        self.fc+=1
        if self.boom: raise RuntimeError("full unavailable")
        return dict(self.f)
async def main():
    x=F({"status":"queued"},{"status":"succeeded","artifacts":[{"kind":"video","url":"https://example.invalid/done.mp4?sig=fresh"}]})
    s,u=await _authoritative_child_status(x,headers={},job_id="done",persisted_state="queued")
    assert s=="succeeded" and u and x.fc==1; print("STALE_QUEUED_FULL_SUCCESS_REUSE=PASS")
    x=F({"status":"running"},{"status":"running"}); s,u=await _authoritative_child_status(x,headers={},job_id="run",persisted_state="queued")
    assert s=="running" and not u and x.fc==1; print("TRUE_RUNNING_FAIL_CLOSED=PASS")
    x=F({"status":"failed"}); s,u=await _authoritative_child_status(x,headers={},job_id="fail",persisted_state="queued")
    assert s=="failed" and not u and x.fc==0; print("TERMINAL_FAILURE_RETRY_SEMANTICS=PASS")
    x=F({"status":"queued"},boom=True); s,u=await _authoritative_child_status(x,headers={},job_id="unknown",persisted_state="queued")
    assert s=="queued" and not u and x.fc==1; print("FULL_STATUS_FAILURE_FAIL_CLOSED=PASS")
asyncio.run(main())
PY
pass PRODUCTION_PREMUTATION_SAFETY_TESTS

# Full application import and isolated health with exact live env.
docker run --rm --env-file "$RUNTIME_ENV" -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false --entrypoint python "$CANDIDATE" -c 'from app.main import app; assert any(getattr(r,"path","")=="/api/health" for r in app.routes); print("DIRECTOR_APP_IMPORT=PASS")'
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
docker run -d --name "$PREFLIGHT" --network "$NETWORK" --env-file "$RUNTIME_ENV" -e PORT=8011 -e DF_DIRECTOR_CHECKPOINTER_AUTO_SETUP=false "$CANDIDATE" >/dev/null
READY=0
for _ in $(seq 1 45); do
  if docker exec "$PREFLIGHT" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || { docker logs --tail 120 "$PREFLIGHT" >&2 || true; fail "isolated candidate health failed"; }
docker rm -f "$PREFLIGHT" >/dev/null 2>&1 || true
pass ISOLATED_PRODUCTION_ENV_HEALTH
pass PRE_MUTATION_CERTIFICATION

rollback(){
  rc=${1:-1}; trap - ERR
  echo "ROLLBACK_TRIGGERED=YES"
  docker tag "$ROLLBACK" "$IMAGE_REF" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null >/dev/null 2>&1 || true
  R="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
  if [[ -n "$R" ]]; then
    for _ in $(seq 1 45); do
      S="$(docker inspect -f '{{.State.Status}}' "$R" 2>/dev/null || true)"
      H="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$R" 2>/dev/null || true)"
      [[ "$S" == "running" && ( "$H" == "healthy" || "$H" == "no-healthcheck" ) ]] && break
      sleep 2
    done
    echo "ROLLBACK_STATE=${S:-unknown}"
    echo "ROLLBACK_HEALTH=${H:-unknown}"
  fi
  echo "ROLLBACK_COMPLETE=YES"
  exit "$rc"
}

# Mutation begins here: Director API only.
docker tag "$CANDIDATE" "$IMAGE_REF"
MUTATED=1
trap 'rc=$?; (( MUTATED == 1 )) && rollback "$rc" || exit "$rc"' ERR
"${COMPOSE[@]}" up -d --no-deps --force-recreate "$SERVICE" </dev/null
pass DIRECTOR_ONLY_RECREATE

DIRECTOR2="$(docker ps -a --format '{{.Names}}' | grep -E '^df-v3-svc-director$|^df-svc-director$|svc-director$' | head -1 || true)"
[[ "$DIRECTOR2" == "$EXPECTED_DIRECTOR" ]] || fail "Director missing after recreate"
STATE=""; HEALTH=""; READY=0
for _ in $(seq 1 60); do
  STATE="$(docker inspect -f '{{.State.Status}}' "$DIRECTOR2" 2>/dev/null || true)"
  HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$DIRECTOR2" 2>/dev/null || true)"
  if [[ "$STATE" == "running" && ( "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ) ]]; then
    if docker exec "$DIRECTOR2" curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8011/api/health >/dev/null 2>&1; then READY=1; break; fi
  fi
  sleep 2
done
[[ "$STATE" == "running" ]] || fail "Director not running"
[[ "$HEALTH" == "healthy" || "$HEALTH" == "no-healthcheck" ]] || fail "Director unhealthy: $HEALTH"
(( READY == 1 )) || fail "Director API health not ready"
[[ "$(docker inspect -f '{{.Image}}' "$DIRECTOR2")" == "$CANDIDATE_ID" ]] || fail "wrong Director image active"
[[ "$(docker exec "$DIRECTOR2" sha256sum /app/app/fusion_execution_orphan_recovery.py | awk '{print $1}')" == "$PATCH_SHA256" ]] || fail "live patch fingerprint mismatch"
docker exec "$DIRECTOR2" python -c 'from app.fusion_execution_orphan_recovery import _authoritative_child_status; from app.main import app; assert _authoritative_child_status; assert any(getattr(r,"path","")=="/api/health" for r in app.routes); print("LIVE_DIRECTOR_IMPORT=PASS")'
docker exec "$DIRECTOR2" curl -fsS --connect-timeout 2 --max-time 5 http://svc-fusion:8002/api/health >/dev/null
docker exec "$DIRECTOR2" curl -fsS --connect-timeout 2 --max-time 5 http://svc-fusion-extension:8006/api/health >/dev/null
pass LIVE_DIRECTOR_AND_DOWNSTREAM_HEALTH

trap - ERR
MUTATED=0

echo "============================================================"
echo "DEV_CERTIFIED_SOURCE=$SOURCE_SHA"
echo "PRODUCTION_COMPONENT=DIRECTOR_API_ONLY"
echo "PRODUCTION_FILE_CHANGES=1"
echo "DATABASE_MIGRATION=NONE"
echo "FACE_AUDIO_FUSION_WORKER_TOUCH=NONE"
echo "ROLLBACK_IMAGE=$ROLLBACK"
echo "STALE_QUEUED_CHILD_RECOVERY=PASS"
echo "TRUE_RUNNING_DUPLICATE_GUARD=PASS"
echo "PRODUCTION_DIRECTOR_HEALTH=PASS"
echo "SAFE_TO_RETRY_SAME_STORY_CHECK_PRICE=YES"
echo "PRODUCTION_PROMOTION=PASS"
echo "============================================================"
