#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

SOURCE_REPO="${SOURCE_REPO:-$PWD}"
SOURCE_REF="${SOURCE_REF:-fix/next3-shared-scene-profile-lock-fix-20260928}"
RUNTIME_ROOT="${RUNTIME_ROOT:-$HOME/workspace/desifaces-runtime}"
ENV_FILE="${RUNTIME_ENV_FILE:-$SOURCE_REPO/infra/.env}"
NETWORK="df-net"
OLD_NETWORK="df-v3-net"
WEB="df-web-dev"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

fail(){ echo "FAIL: $*" >&2; exit 1; }

[[ -d "$SOURCE_REPO/.git" || -f "$SOURCE_REPO/.git" ]] || fail "source repo is not a Git checkout: $SOURCE_REPO"
[[ -f "$ENV_FILE" ]] || fail "missing runtime env: $ENV_FILE"

echo "===== 0. CLEAN SOURCE WORKTREE ====="
git -C "$SOURCE_REPO" fetch --quiet origin "$SOURCE_REF"
SOURCE_SHA="$(git -C "$SOURCE_REPO" rev-parse FETCH_HEAD)"

if [[ -e "$RUNTIME_ROOT" ]]; then
  git -C "$RUNTIME_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1     || fail "runtime root exists but is not a Git worktree: $RUNTIME_ROOT"
  [[ -z "$(git -C "$RUNTIME_ROOT" status --porcelain)" ]]     || fail "runtime worktree is dirty: $RUNTIME_ROOT"
  git -C "$RUNTIME_ROOT" checkout --quiet --detach "$SOURCE_SHA"
else
  mkdir -p "$(dirname "$RUNTIME_ROOT")"
  git -C "$SOURCE_REPO" worktree add --quiet --detach "$RUNTIME_ROOT" "$SOURCE_SHA"
fi

PROJECT_ROOT="$RUNTIME_ROOT"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"
[[ -f "$COMPOSE_FILE" ]] || fail "canonical compose missing from clean worktree"

export DESIFACES_RUNTIME_ENV_FILE="$ENV_FILE"

echo "SOURCE_SHA=$SOURCE_SHA"
echo "PROJECT_ROOT=$PROJECT_ROOT"
echo "COMPOSE_FILE=$COMPOSE_FILE"
echo "CLEAN_SOURCE_WORKTREE=PASS"

DC=(docker compose
  --project-directory "$PROJECT_ROOT"
  --env-file "$ENV_FILE"
  -f "$COMPOSE_FILE"
  --profile execution
  --profile orchestration
)

echo "============================================================"
echo " desifaces DEV — CLEAN CANONICAL RECREATE"
echo "============================================================"
echo "compose=$COMPOSE_FILE"
echo "source_sha=$SOURCE_SHA"
echo "runtime_root=$PROJECT_ROOT"
echo "network=$NETWORK"
echo "production=UNTOUCHED"

echo
echo "===== 1. CANONICAL COMPOSE GATE ====="
"${DC[@]}" config -q
SERVICES_CONFIG="$("${DC[@]}" config --services)"
if grep -Eiq 'v3|(^|[-_.])v[0-9]+($|[-_.])' <<<"$SERVICES_CONFIG"; then
  echo "$SERVICES_CONFIG"
  fail "version-specific compose service remains"
fi
echo "CANONICAL_COMPOSE=PASS"

echo
echo "===== 2. SNAPSHOT CURRENT CERTIFIED IMAGE IDS ====="
declare -A SERVICE_CONTAINER=(
  [svc-core]=df-svc-core
  [svc-fusion]=df-svc-fusion
  [svc-fusion-worker]=df-svc-fusion-worker
  [svc-face]=df-svc-face
  [svc-face-worker]=df-svc-face-worker
  [svc-pricing]=df-svc-pricing
  [svc-commerce]=df-svc-commerce
  [svc-commerce-worker]=df-svc-commerce-worker
  [svc-audio]=df-svc-audio
  [svc-audio-worker]=df-svc-audio-worker
  [svc-dashboard]=df-svc-dashboard
  [svc-dashboard-worker]=df-svc-dashboard-worker
  [svc-fusion-extension]=df-svc-fusion-extension
  [svc-fusion-extension-worker]=df-svc-fusion-extension-worker
  [svc-fusion-extension-stitch-worker]=df-svc-fusion-extension-stitch-worker
  [svc-music]=df-svc-music
  [svc-music-worker]=df-svc-music-worker
  [svc-marketing]=df-svc-marketing
  [svc-marketing-worker]=df-svc-marketing-worker
  [svc-marketing-scheduler]=df-svc-marketing-scheduler
  [svc-studio-coach-refresh-worker]=df-svc-studio-coach-refresh-worker
  [svc-director]=df-svc-director
)

for SVC in "${!SERVICE_CONTAINER[@]}"; do
  C="${SERVICE_CONTAINER[$SVC]}"
  docker inspect "$C" >/dev/null 2>&1 || fail "required current container missing: $C"
  ID="$(docker inspect "$C" --format '{{.Image}}')"
  echo "$SVC $C $ID"
  docker tag "$ID" "desifaces-${SVC}:latest"
done

# Director worker intentionally follows the certified Director API image because
# the canonical Compose contract uses one Director image for API + worker.
DIRECTOR_ID="$(docker inspect df-svc-director --format '{{.Image}}')"
docker tag "$DIRECTOR_ID" desifaces-svc-director:latest
echo "DIRECTOR_CERTIFIED_IMAGE=$DIRECTOR_ID"

echo
echo "===== 3. ASSISTANT IMAGE GATE ====="
if ! docker image inspect desifaces-svc-assistant:latest >/dev/null 2>&1; then
  LEGACY_ASSISTANT_ID="$(
    docker image ls --format '{{.Repository}} {{.ID}}' |
      awk '$1=="desifaces-v3-svc-assistant"{print $2; exit}'
  )"
  if [[ -n "$LEGACY_ASSISTANT_ID" ]]; then
    docker tag "$LEGACY_ASSISTANT_ID" desifaces-svc-assistant:latest
    echo "ASSISTANT_IMAGE=RETAGGED_EXISTING"
  else
    echo "ASSISTANT_IMAGE=BUILD_REQUIRED"
    docker build \
      -t desifaces-svc-assistant:latest \
      -f "$PROJECT_ROOT/services/svc-assistant/app/Dockerfile.v3" \
      "$PROJECT_ROOT"
  fi
fi
docker image inspect desifaces-svc-assistant:latest >/dev/null
[[ "$(docker image inspect desifaces-svc-director:latest --format '{{.Id}}')" == "$DIRECTOR_ID" ]] \
  || fail "Director image tag changed during Assistant build"
echo "ASSISTANT_IMAGE=PASS"
echo "DIRECTOR_IMAGE_PRESERVED=PASS"

echo
echo "===== 4. SNAPSHOT WEB ====="
docker inspect "$WEB" >/dev/null 2>&1 || fail "$WEB missing"
WEB_IMAGE="$(docker inspect "$WEB" --format '{{.Image}}')"
WEB_ENV="/tmp/df-web-dev-${STAMP}.env"
umask 077
docker inspect "$WEB" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$WEB_ENV"
python3 - "$WEB_ENV" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
rows={}
order=[]
for raw in p.read_text().splitlines():
    if "=" not in raw:
        continue
    k,v=raw.split("=",1)
    if k not in rows:
        order.append(k)
    rows[k]=v
updates={
    "CORE_BASE_URL":"http://svc-core:8000",
    "DIRECTOR_BASE_URL":"http://svc-director:8011",
    "FACE_BASE_URL":"http://svc-face:8003",
    "AUDIO_BASE_URL":"http://svc-audio:8004",
    "FUSION_BASE_URL":"http://svc-fusion:8002",
    "FUSION_EXTENSION_BASE_URL":"http://svc-fusion-extension:8006",
    "PRICING_BASE_URL":"http://svc-pricing:8009",
    "COMMERCE_BASE_URL":"http://svc-commerce:8008",
    "DASHBOARD_BASE_URL":"http://svc-dashboard:8005",
    "ASSISTANT_BASE_URL":"http://svc-assistant:8012",
    "NOTIFICATION_BASE_URL":"http://svc-core:8000",
}
for k,v in updates.items():
    if k not in rows:
        order.append(k)
    rows[k]=v
p.write_text("\n".join(f"{k}={rows[k]}" for k in order)+"\n")
PY
echo "WEB_IMAGE=$WEB_IMAGE"

echo
echo "===== 5. REMOVE CANONICAL APP CONTAINERS ONLY ====="
APP_CONTAINERS=(
  df-svc-core
  df-svc-fusion
  df-svc-fusion-worker
  df-svc-face
  df-svc-face-worker
  df-svc-pricing
  df-svc-commerce
  df-svc-commerce-worker
  df-svc-audio
  df-svc-audio-worker
  df-svc-dashboard
  df-svc-dashboard-worker
  df-svc-fusion-extension
  df-svc-fusion-extension-worker
  df-svc-fusion-extension-stitch-worker
  df-svc-music
  df-svc-music-worker
  df-svc-marketing
  df-svc-marketing-worker
  df-svc-marketing-scheduler
  df-svc-studio-coach-refresh-worker
  df-svc-director
  df-svc-director-worker
  df-svc-assistant
  df-web-dev
)

for C in "${APP_CONTAINERS[@]}"; do
  if docker inspect "$C" >/dev/null 2>&1; then
    echo "remove $C"
    docker rm -f "$C" >/dev/null
  fi
done

echo
echo "===== 6. CANONICAL NETWORK ONLY ====="
docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

for C in desifaces-db desifaces-redis; do
  docker inspect "$C" >/dev/null 2>&1 || fail "$C missing"
  if ! docker inspect "$C" --format '{{json .NetworkSettings.Networks}}' | grep -q '"df-net"'; then
    docker network connect --alias "$C" "$NETWORK" "$C"
  fi
done

if docker network inspect "$OLD_NETWORK" >/dev/null 2>&1; then
  while read -r C; do
    [[ -n "$C" ]] || continue
    echo "disconnect legacy network: $C"
    docker network disconnect -f "$OLD_NETWORK" "$C" >/dev/null 2>&1 || true
  done < <(
    docker network inspect "$OLD_NETWORK"       --format '{{range $id,$v := .Containers}}{{println $v.Name}}{{end}}'
  )
  docker network rm "$OLD_NETWORK" >/dev/null
fi
echo "LEGACY_NETWORK_REMOVED=PASS"

echo
echo "===== 7. RECREATE APPLICATION FROM SINGLE COMPOSE ====="
APP_SERVICES=(
  svc-core
  svc-fusion
  svc-fusion-worker
  svc-face
  svc-face-worker
  svc-pricing
  svc-commerce
  svc-commerce-worker
  svc-audio
  svc-audio-worker
  svc-dashboard
  svc-dashboard-worker
  svc-fusion-extension
  svc-fusion-extension-worker
  svc-fusion-extension-stitch-worker
  svc-music
  svc-music-worker
  svc-marketing
  svc-marketing-worker
  svc-marketing-scheduler
  svc-studio-coach-refresh-worker
  svc-director
  svc-director-worker
  svc-assistant
)

"${DC[@]}" up -d   --no-build   --no-deps   --force-recreate   "${APP_SERVICES[@]}"

echo
echo "===== 8. RECREATE WEB ====="
docker run -d   --name "$WEB"   --network "$NETWORK"   --network-alias web-dev   --env-file "$WEB_ENV"   -p 127.0.0.1:13000:3000   --restart unless-stopped   "$WEB_IMAGE" >/dev/null
rm -f "$WEB_ENV"

echo
echo "===== 9. API HEALTH ====="
declare -A PORTS=(
  [df-svc-core]=18000
  [df-svc-fusion]=18002
  [df-svc-face]=18003
  [df-svc-audio]=18004
  [df-svc-dashboard]=18005
  [df-svc-fusion-extension]=18006
  [df-svc-music]=18007
  [df-svc-commerce]=18008
  [df-svc-pricing]=18009
  [df-svc-marketing]=18010
  [df-svc-director]=18011
  [df-svc-assistant]=18012
)

for C in "${!PORTS[@]}"; do
  P="${PORTS[$C]}"
  CODE=""
  for _ in $(seq 1 60); do
    CODE="$(curl -sS -o /dev/null -w '%{http_code}'       --connect-timeout 2 --max-time 4       "http://127.0.0.1:${P}/api/health" 2>/dev/null || true)"
    [[ "$CODE" == "200" ]] && break
    sleep 2
  done
  [[ "$CODE" == "200" ]] || {
    echo "----- $C LAST LOGS -----"
    docker logs --tail 60 "$C" 2>&1 || true
    fail "$C health HTTP_${CODE:-NO_RESPONSE}"
  }
  echo "PASS $C HTTP_200"
done

WEB_CODE=""
for _ in $(seq 1 60); do
  WEB_CODE="$(curl -sS -o /dev/null -w '%{http_code}'     --connect-timeout 2 --max-time 4     http://127.0.0.1:13000/auth/login 2>/dev/null || true)"
  [[ "$WEB_CODE" == "200" ]] && break
  sleep 2
done
[[ "$WEB_CODE" == "200" ]] || fail "web health HTTP_${WEB_CODE:-NO_RESPONSE}"
echo "PASS df-web-dev HTTP_200"

echo
echo "===== 10. RESTART LOOP GATE ====="
RESTARTING="$(
  docker ps --format '{{.Names}} {{.Status}}' |
    grep -E '^(df-svc-|df-web-dev ).*Restarting' || true
)"
[[ -z "$RESTARTING" ]] || {
  echo "$RESTARTING"
  fail "restart loops remain"
}
echo "NO_RESTART_LOOPS=PASS"

echo
echo "===== 11. WEB DNS CONTRACT ====="
timeout 30s docker exec -i "$WEB" node - <<'NODE'
const dns=require("node:dns").promises;
const keys=[
  "CORE_BASE_URL","DIRECTOR_BASE_URL","FACE_BASE_URL","AUDIO_BASE_URL",
  "FUSION_BASE_URL","FUSION_EXTENSION_BASE_URL","PRICING_BASE_URL",
  "COMMERCE_BASE_URL","DASHBOARD_BASE_URL","ASSISTANT_BASE_URL"
];
(async()=>{
  for(const key of keys){
    const raw=(process.env[key]||"").replace(/\/+$/,"");
    if(!raw) throw new Error(key+"_MISSING");
    const host=new URL(raw).hostname;
    const found=await dns.lookup(host);
    const r=await fetch(raw+"/api/health");
    console.log(key+" "+host+" "+found.address+" HTTP_"+r.status);
    if(r.status!==200) process.exit(2);
  }
})().catch(e=>{console.error(e);process.exit(3)});
NODE
echo "WEB_DNS_CONTRACT=PASS"

echo
echo "===== 12. ZERO-V3 RUNTIME GATE ====="
BAD_C="$(docker ps -a --format '{{.Names}}' | grep -i 'v3' || true)"
BAD_N="$(docker network ls --format '{{.Name}}' | grep -i 'v3' || true)"
[[ -z "$BAD_C" ]] || { echo "$BAD_C"; fail "v3 container names remain"; }
[[ -z "$BAD_N" ]] || { echo "$BAD_N"; fail "v3 network names remain"; }
echo "ZERO_V3_CONTAINERS=PASS"
echo "ZERO_V3_NETWORKS=PASS"

echo
echo "===== FINAL RUNTIME ====="
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | sort

echo
echo "============================================================"
echo " DESIFACES_DEV_CANONICAL_RECREATE=PASS"
echo " source_compose=docker-compose.yml"
echo " source_sha=$SOURCE_SHA"
echo " runtime_root=$PROJECT_ROOT"
echo " network=df-net"
echo " certified_director_image=$DIRECTOR_ID"
echo " production=UNTOUCHED"
echo " database=PRESERVED"
echo " redis=PRESERVED"
echo "============================================================"
