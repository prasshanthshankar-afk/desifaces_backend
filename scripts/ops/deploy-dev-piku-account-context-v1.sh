#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }
TARGET_SHA="${TARGET_SHA:?TARGET_SHA is required}"
SHORT="${TARGET_SHA:0:12}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "============================================================"
echo " desifaces DEV — PIKU ACCOUNT CONTEXT V1"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "scope=svc-assistant_only"
echo "dashboard_ui=SEPARATE_DEPLOYMENT"
echo "generation_services=UNTOUCHED"
echo "pricing_service=UNTOUCHED"
echo "production=UNTOUCHED"

REPO=""
for p in "$HOME/workspace/desifaces-runtime" "$HOME/workspace/desifaces-v3" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-backend"; do
  if [[ -d "$p/.git" || -f "$p/.git" ]]; then
    remote="$(git -C "$p" remote get-url origin 2>/dev/null || true)"
    if [[ "$remote" == *"prasshanthshankar-afk/desifaces_backend"* ]]; then
      REPO="$p"
      break
    fi
  fi
done
[[ -n "$REPO" ]] || { echo "FAIL: backend repo not found"; exit 2; }

CURRENT=""
for c in df-svc-assistant df-v3-svc-assistant; do
  if docker inspect "$c" >/dev/null 2>&1; then
    CURRENT="$c"
    break
  fi
done
[[ -n "$CURRENT" ]] || { echo "FAIL: live DEV assistant container not found"; exit 2; }
[[ "$(docker inspect -f '{{.State.Running}}' "$CURRENT")" == "true" ]] || { echo "FAIL: $CURRENT not running"; exit 2; }

WT="/tmp/df-piku-account-context-$SHORT"
rm -rf "$WT"
git -C "$REPO" fetch --no-tags origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$REPO" cat-file -e "$TARGET_SHA^{commit}"
git -C "$REPO" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null

ENV_FILE="/tmp/df-piku-account-context-$SHORT.env"
CANDIDATE="df-piku-account-context-candidate-$SHORT"
cleanup(){
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo
echo "===== 1. SOURCE CONTRACT ====="
python3 -m compileall -q   "$WT/services/svc-assistant/app/app"   "$WT/services/shared/python/desifaces_shared/identity"
grep -Fq 'context_freshness": "request_time_read_only"' "$WT/services/svc-assistant/app/app/context.py"
grep -Fq '_fetch_spending_summary' "$WT/services/svc-assistant/app/app/context.py"
grep -Fq '_fetch_dashboard_library' "$WT/services/svc-assistant/app/app/context.py"
grep -Fq '_fetch_notifications' "$WT/services/svc-assistant/app/app/context.py"
grep -Fq '_fetch_recent_stories' "$WT/services/svc-assistant/app/app/context.py"
grep -Fq 'operational_account_answer' "$WT/services/svc-assistant/app/app/service.py"
grep -Fq 'href: str | None' "$WT/services/svc-assistant/app/app/schemas.py"
echo "PIKU_ACCOUNT_CONTEXT_SOURCE=PASS"

echo
echo "===== 2. BUILD ASSISTANT CANDIDATE ====="
IMAGE="df-piku-account-context:$SHORT"
docker build   -f "$WT/services/svc-assistant/app/Dockerfile.v3"   -t "$IMAGE"   "$WT"   >/tmp/df-piku-account-context-build.log 2>&1
echo "PIKU_ACCOUNT_CONTEXT_BUILD=PASS image=$IMAGE"

echo
echo "===== 3. FOCUSED TESTS IN CANDIDATE IMAGE ====="
docker run --rm   -v "$WT/services/svc-assistant/app/tests:/tests:ro"   "$IMAGE"   sh -lc 'pip install -q pytest >/dev/null && pytest -q /tests'
echo "PIKU_ACCOUNT_CONTEXT_TESTS=PASS"

echo
echo "===== 4. CAPTURE CURRENT DEV ASSISTANT ENV ====="
python3 - "$CURRENT" "$ENV_FILE" <<'PY'
import json, os, subprocess, sys
container,path=sys.argv[1:]
obj=json.loads(subprocess.check_output(["docker","inspect",container]))[0]
values={}
order=[]
for item in obj["Config"].get("Env") or []:
    if "=" not in item:
        continue
    key,value=item.split("=",1)
    if key not in values:
        order.append(key)
    values[key]=value
values["DF_CORE_BASE_URL"]="http://svc-core:8000"
values.setdefault("DF_ASSISTANT_LIBRARY_LIMIT","40")
values.setdefault("DF_ASSISTANT_RECENT_STORIES_LIMIT","10")
values.setdefault("DF_ASSISTANT_NOTIFICATIONS_LIMIT","20")
for key in ("DF_CORE_BASE_URL","DF_ASSISTANT_LIBRARY_LIMIT","DF_ASSISTANT_RECENT_STORIES_LIMIT","DF_ASSISTANT_NOTIFICATIONS_LIMIT"):
    if key not in order:
        order.append(key)
with open(path,"w",encoding="utf-8") as fh:
    for key in order:
        value=values[key]
        if "\n" in value or "\r" in value:
            raise SystemExit("invalid environment newline")
        fh.write(f"{key}={value}\n")
os.chmod(path,0o600)
PY
echo "PIKU_DEV_ENV_CAPTURE=PASS"

NETWORK="$(docker inspect "$CURRENT" -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' | sed '/^$/d' | head -n1)"
[[ -n "$NETWORK" ]] || { echo "FAIL: assistant network unavailable"; exit 2; }

echo
echo "===== 5. CANDIDATE RUNTIME ====="
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
docker run -d --rm   --name "$CANDIDATE"   --network "$NETWORK"   --env-file "$ENV_FILE"   -p 127.0.0.1:18013:8012   "$IMAGE" >/dev/null

HTTP=000
for i in $(seq 1 45); do
  HTTP="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/df-piku-candidate-health.json -w '%{http_code}' http://127.0.0.1:18013/api/health 2>/dev/null || true)"
  echo "candidate_wait=$i http=$HTTP"
  [[ "$HTTP" == "200" ]] && break
  sleep 2
done
[[ "$HTTP" == "200" ]] || { docker logs --tail 160 "$CANDIDATE"; echo "FAIL: assistant candidate health"; exit 1; }
python3 - <<'PY'
import json
p=json.load(open("/tmp/df-piku-candidate-health.json"))
assert p.get("service")=="svc-assistant", p
live=str(p.get("live_context") or "")
for marker in ("dashboard","library","pricing","spending","notifications","recent_stories","user_scoped_generation","director_story"):
    assert marker in live, (marker,live)
print("PIKU_CANDIDATE_HEALTH_CONTRACT=PASS")
PY

echo
echo "===== 6. TARGETED CUTOVER WITH ROLLBACK ====="
OLD_IMAGE_ID="$(docker inspect "$CURRENT" -f '{{.Image}}')"
LIVE_REF="$(docker inspect "$CURRENT" -f '{{.Config.Image}}')"
[[ -n "$LIVE_REF" && "$LIVE_REF" != *@* ]] || { echo "FAIL: unsupported live image ref=$LIVE_REF"; exit 2; }
ROLLBACK_IMAGE="df-piku-account-context-rollback:$STAMP"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE"
docker tag "$IMAGE" "$LIVE_REF"

PROJECT="$(docker inspect "$CURRENT" --format '{{ index .Config.Labels "com.docker.compose.project" }}')"
SERVICE="$(docker inspect "$CURRENT" --format '{{ index .Config.Labels "com.docker.compose.service" }}')"
WORKDIR="$(docker inspect "$CURRENT" --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
FILES="$(docker inspect "$CURRENT" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
[[ -n "$PROJECT" && -n "$SERVICE" && -n "$WORKDIR" && -n "$FILES" ]] || { echo "FAIL: compose provenance unavailable"; exit 2; }

compose_recreate(){
  local args=(-p "$PROJECT")
  local IFS=','
  read -ra fs <<< "$FILES"
  for file in "${fs[@]}"; do args+=(-f "$file"); done
  ( cd "$WORKDIR" && docker compose "${args[@]}" up -d --no-deps --force-recreate --pull never --no-build "$SERVICE" )
}

rollback(){
  rc=$?
  set +e
  docker tag "$ROLLBACK_IMAGE" "$LIVE_REF" >/dev/null 2>&1 || true
  compose_recreate >/dev/null 2>&1 || true
  echo "PIKU_DEV_ROLLBACK=COMPLETE"
  exit "$rc"
}
trap rollback ERR

compose_recreate >/dev/null

LIVE_PORT="$(docker port "$CURRENT" 8012/tcp 2>/dev/null | head -n1 | sed -E 's#.*:([0-9]+)$#\1#')"
[[ -n "$LIVE_PORT" ]] || LIVE_PORT=18012
LIVE_HTTP=000
for i in $(seq 1 45); do
  LIVE_HTTP="$(curl -sS --connect-timeout 2 --max-time 5 -o /tmp/df-piku-live-health.json -w '%{http_code}' "http://127.0.0.1:${LIVE_PORT}/api/health" 2>/dev/null || true)"
  echo "live_wait=$i http=$LIVE_HTTP"
  [[ "$LIVE_HTTP" == "200" ]] && break
  sleep 2
done
[[ "$LIVE_HTTP" == "200" ]] || { docker logs --tail 160 "$CURRENT"; false; }

python3 - <<'PY'
import json
p=json.load(open("/tmp/df-piku-live-health.json"))
assert p.get("service")=="svc-assistant", p
live=str(p.get("live_context") or "")
for marker in ("dashboard","library","pricing","spending","notifications","recent_stories","user_scoped_generation","director_story"):
    assert marker in live, (marker,live)
print("PIKU_LIVE_ACCOUNT_CONTEXT_HEALTH=PASS")
PY

docker exec "$CURRENT" sh -lc   "grep -Fq '_fetch_spending_summary' /app/app/context.py &&    grep -Fq 'operational_account_answer' /app/app/service.py &&    grep -Fq 'request_time_read_only' /app/app/context.py"
echo "PIKU_LIVE_SOURCE_CONTRACT=PASS"

trap - ERR

echo
echo "============================================================"
echo "PIKU_ACCOUNT_CONTEXT_V1=PASS"
echo "active_container=$CURRENT"
echo "active_image=$LIVE_REF"
echo "rollback_image=$ROLLBACK_IMAGE"
echo "generation_services=UNTOUCHED"
echo "pricing_service=UNTOUCHED"
echo "production=UNTOUCHED"
echo "============================================================"
