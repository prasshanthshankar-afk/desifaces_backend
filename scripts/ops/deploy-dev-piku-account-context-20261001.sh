#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }
TARGET_SHA="${TARGET_SHA:?TARGET_SHA is required}"
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "FAIL: exact TARGET_SHA required"; exit 2; }

CONTAINER="${PIKU_CONTAINER:-df-svc-assistant}"
CANDIDATE="df-svc-assistant-account-context-candidate"
SHORT="${TARGET_SHA:0:12}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WT="/tmp/df-piku-account-context-$SHORT"
ENV_FILE="/tmp/df-piku-account-context-$SHORT.env"
CANDIDATE_IMAGE="desifaces-svc-assistant:account-context-$SHORT"

fail(){ echo "FAIL: $*" >&2; exit 1; }
cleanup(){
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
  if [[ -n "${REPO:-}" && ( -d "$REPO/.git" || -f "$REPO/.git" ) ]]; then
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "assistant container missing: $CONTAINER"
[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" == "true" ]] || fail "assistant container is not running"

echo "============================================================"
echo " desifaces DEV — PIKU ACCOUNT CONTEXT DEPLOY"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "target_container=$CONTAINER"
echo "scope=ASSISTANT_ONLY"
echo "generation_services=UNTOUCHED"
echo "pricing_service=UNTOUCHED"
echo "production=UNTOUCHED"

REPO=""
for p in "$HOME/workspace/desifaces-runtime" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-backend"; do
  [[ -d "$p/.git" || -f "$p/.git" ]] || continue
  remote="$(git -C "$p" remote get-url origin 2>/dev/null || true)"
  if [[ "$remote" == *"prasshanthshankar-afk/desifaces_backend"* ]]; then
    REPO="$p"
    break
  fi
done
[[ -n "$REPO" ]] || fail "backend repository not found"

PROJECT="$(docker inspect "$CONTAINER" -f '{{ index .Config.Labels "com.docker.compose.project" }}')"
SERVICE="$(docker inspect "$CONTAINER" -f '{{ index .Config.Labels "com.docker.compose.service" }}')"
WORKDIR="$(docker inspect "$CONTAINER" -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
CONFIG_FILES="$(docker inspect "$CONTAINER" -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
LIVE_REF="$(docker inspect "$CONTAINER" -f '{{.Config.Image}}')"
OLD_IMAGE_ID="$(docker inspect "$CONTAINER" -f '{{.Image}}')"
NETWORK="$(docker inspect "$CONTAINER" -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' | sed '/^$/d' | head -n1)"

[[ -n "$PROJECT" && "$PROJECT" != "<no value>" ]] || fail "compose project ownership missing"
[[ -n "$SERVICE" && "$SERVICE" != "<no value>" ]] || fail "compose service ownership missing"
[[ -d "$WORKDIR" ]] || fail "compose working directory unavailable: $WORKDIR"
[[ -n "$CONFIG_FILES" && "$CONFIG_FILES" != "<no value>" ]] || fail "compose config files unavailable"
[[ -n "$NETWORK" ]] || fail "assistant network unavailable"
[[ "$LIVE_REF" != *@* ]] || fail "digest-only live image ref is unsupported for safe cutover"

COMPOSE_ENV_FILE=""
for candidate in \
  "$WORKDIR/infra/.env" \
  "$WORKDIR/.env" \
  "$HOME/workspace/desifaces-runtime/infra/.env" \
  "$HOME/workspace/desifaces-runtime/.env"
do
  if [[ -f "$candidate" ]]; then
    COMPOSE_ENV_FILE="$candidate"
    break
  fi
done
[[ -n "$COMPOSE_ENV_FILE" ]] || fail "live desifaces-runtime Compose env file not found"

echo "COMPOSE_OWNERSHIP=PASS project=$PROJECT service=$SERVICE"
echo "assistant_network=$NETWORK"
echo "live_image_ref=$LIVE_REF"
echo "compose_env_source=$COMPOSE_ENV_FILE"

git -C "$REPO" fetch --no-tags origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$REPO" cat-file -e "$TARGET_SHA^{commit}"
git -C "$REPO" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null
[[ "$(git -C "$WT" rev-parse HEAD)" == "$TARGET_SHA" ]] || fail "worktree SHA mismatch"

echo
echo "===== 1. SOURCE CONTRACT ====="
python3 -m py_compile   "$WT/services/svc-assistant/app/app/context.py"   "$WT/services/svc-assistant/app/app/service.py"   "$WT/services/svc-assistant/app/app/schemas.py"   "$WT/services/svc-assistant/app/app/main.py"   "$WT/services/svc-assistant/app/app/llm.py"
grep -q '_fetch_spending_summary' "$WT/services/svc-assistant/app/app/context.py"
grep -q '_fetch_dashboard_library' "$WT/services/svc-assistant/app/app/context.py"
grep -q 'operational_account_answer' "$WT/services/svc-assistant/app/app/service.py"
grep -q '/api/assistant/context' "$WT/services/svc-assistant/app/app/main.py"
echo "PIKU_ACCOUNT_CONTEXT_SOURCE=PASS"

echo
echo "===== 2. BUILD EXACT CANDIDATE ====="
docker build   -f "$WT/services/svc-assistant/app/Dockerfile.v3"   -t "$CANDIDATE_IMAGE"   "$WT"
echo "PIKU_ACCOUNT_CONTEXT_BUILD=PASS image=$CANDIDATE_IMAGE"

python3 - "$CONTAINER" "$ENV_FILE" <<'PY'
import json, os, subprocess, sys
container,path=sys.argv[1:]
obj=json.loads(subprocess.check_output(["docker","inspect",container]))[0]
with open(path,"w",encoding="utf-8") as f:
    for item in obj["Config"].get("Env") or []:
        if "\n" in item or "\r" in item:
            raise SystemExit("invalid environment newline")
        f.write(item+"\n")
os.chmod(path,0o600)
PY
echo "ASSISTANT_ENV_CAPTURE=PASS"

echo
echo "===== 3. ISOLATED CANDIDATE ====="
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
docker run -d --rm   --name "$CANDIDATE"   --network "$NETWORK"   --env-file "$ENV_FILE"   "$CANDIDATE_IMAGE" >/dev/null

HTTP=000
for i in $(seq 1 45); do
  HTTP="$(docker exec "$CANDIDATE" sh -lc "curl -sS -o /tmp/health.json -w '%{http_code}' --max-time 4 http://127.0.0.1:8012/api/health 2>/dev/null || true")"
  echo "candidate_wait=$i http=$HTTP"
  [[ "$HTTP" == "200" ]] && break
  sleep 2
done
[[ "$HTTP" == "200" ]] || fail "assistant candidate health failed"
docker exec "$CANDIDATE" sh -lc "grep -q 'pricing_spending+saved_work' /tmp/health.json"
docker exec "$CANDIDATE" sh -lc "grep -q '/api/assistant/context' /app/app/main.py"
echo "PIKU_ACCOUNT_CONTEXT_CANDIDATE=PASS"

echo
echo "===== 4. CUTOVER ONLY ASSISTANT ====="
ROLLBACK_IMAGE="desifaces-svc-assistant:rollback-$STAMP"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE"
docker tag "$CANDIDATE_IMAGE" "$LIVE_REF"

compose_with_live_env(){
  local mode="$1"
  python3 - "$ENV_FILE" "$COMPOSE_ENV_FILE" "$WORKDIR" "$PROJECT" "$CONFIG_FILES" "$SERVICE" "$mode" <<'PY'
import os, subprocess, sys

assistant_env_file, compose_env_file, workdir, project, config_files, service, mode = sys.argv[1:]
env = os.environ.copy()

with open(assistant_env_file, "r", encoding="utf-8") as f:
    for raw in f:
        line = raw.rstrip("\n")
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value

cmd = ["docker", "compose", "--env-file", compose_env_file, "-p", project]
for file in [x.strip() for x in config_files.split(",") if x.strip()]:
    cmd += ["-f", file]

if mode == "config":
    probe = subprocess.run(
        cmd + ["config", "--services"],
        cwd=workdir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if probe.returncode != 0:
        sys.stderr.write(probe.stderr)
        raise SystemExit(probe.returncode)
    services = {x.strip() for x in probe.stdout.splitlines() if x.strip()}
    if service not in services:
        raise SystemExit(f"compose service missing after interpolation: {service}")
    print("COMPOSE_LIVE_ENV_INTERPOLATION=PASS")
elif mode == "up":
    subprocess.run(
        cmd + [
            "up", "-d", "--no-deps", "--force-recreate",
            "--pull", "never", "--no-build", service
        ],
        cwd=workdir,
        env=env,
        check=True,
    )
else:
    raise SystemExit(f"unsupported compose mode: {mode}")
PY
}

echo "===== 4A. COMPOSE INTERPOLATION WITH LIVE RUNTIME + ASSISTANT ENV ====="
compose_with_live_env config

rollback(){
  rc=$?
  trap - ERR
  set +e
  echo "PIKU_ACCOUNT_CONTEXT_ROLLBACK=START"
  docker tag "$ROLLBACK_IMAGE" "$LIVE_REF" >/dev/null 2>&1 || true
  compose_with_live_env up >/dev/null 2>&1 || true
  echo "PIKU_ACCOUNT_CONTEXT_ROLLBACK=COMPLETE"
  exit "$rc"
}
trap rollback ERR

compose_with_live_env up

LIVE_HTTP=000
for i in $(seq 1 45); do
  LIVE_HTTP="$(docker exec "$CONTAINER" sh -lc "curl -sS -o /tmp/health.json -w '%{http_code}' --max-time 4 http://127.0.0.1:8012/api/health 2>/dev/null || true")"
  echo "live_wait=$i http=$LIVE_HTTP"
  [[ "$LIVE_HTTP" == "200" ]] && break
  sleep 2
done
[[ "$LIVE_HTTP" == "200" ]] || fail "assistant failed health after cutover"
[[ "$(docker inspect "$CONTAINER" -f '{{.Config.Image}}')" == "$LIVE_REF" ]] || fail "assistant live image ref changed unexpectedly"
docker exec "$CONTAINER" sh -lc "grep -q 'pricing_spending+saved_work' /tmp/health.json"
docker exec "$CONTAINER" sh -lc "grep -q '_fetch_spending_summary' /app/app/context.py"
docker exec "$CONTAINER" sh -lc "grep -q '_fetch_dashboard_library' /app/app/context.py"
echo "PIKU_ACCOUNT_CONTEXT_LIVE=PASS"

trap - ERR

echo
echo "============================================================"
echo "PIKU_ACCOUNT_CONTEXT_DEPLOY=PASS"
echo "assistant_container=$CONTAINER"
echo "assistant_image=$LIVE_REF"
echo "rollback_image=$ROLLBACK_IMAGE"
echo "generation_services=UNTOUCHED"
echo "pricing_service=UNTOUCHED"
echo "production=UNTOUCHED"
echo "============================================================"
