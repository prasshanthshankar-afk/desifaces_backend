#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: this script is DEV-only"
  exit 1
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="/tmp/desifaces-clean-runtime-${STAMP}.log"
OLD_NET="df-v3-net"
NEW_NET="df-net"
WEB="df-web-dev"

exec > >(tee "$LOG") 2>&1

echo "============================================================"
echo " desifaces DEV — CLEAN CANONICAL RUNTIME START"
echo "============================================================"
echo "log=$LOG"

echo
echo "===== 1. CURRENT RUNTIME SNAPSHOT ====="
docker ps -a --format 'table {{.Names}}	{{.Image}}	{{.Status}}'

echo
echo "===== 2. NORMALIZE RUNNING CONTAINER NAMES ====="

while read -r OLD; do
  [[ -n "$OLD" ]] || continue
  NEW=""

  case "$OLD" in
    df-v3-svc-*)
      NEW="df-svc-${OLD#df-v3-svc-}"
      ;;
    df-v3-web-dev|df-v3-web)
      NEW="df-web-dev"
      ;;
    df-v3-web-prod)
      echo "FAIL: production-style web container found on DEV: $OLD"
      exit 1
      ;;
    *)
      echo "FAIL: unrecognized running v3 container: $OLD"
      exit 1
      ;;
  esac

  if docker inspect "$NEW" >/dev/null 2>&1; then
    echo "FAIL: both legacy and canonical containers exist: $OLD / $NEW"
    exit 1
  fi

  echo "rename $OLD -> $NEW"
  docker rename "$OLD" "$NEW"
done < <(docker ps --format '{{.Names}}' | grep -i 'v3' || true)

echo
echo "===== 3. CANONICAL NETWORK ====="
docker network inspect "$NEW_NET" >/dev/null 2>&1 || docker network create "$NEW_NET" >/dev/null
echo "network=$NEW_NET"

alias_for(){
  case "$1" in
    desifaces-db) echo "desifaces-db" ;;
    desifaces-redis) echo "desifaces-redis" ;;
    df-web-dev) echo "web-dev" ;;
    df-svc-*) echo "${1#df-}" ;;
    *) echo "" ;;
  esac
}

echo
echo "===== 4. ATTACH EVERY LIVE DESIFACES CONTAINER TO df-net WITH CANONICAL DNS ====="

mapfile -t LIVE < <(
  docker ps --format '{{.Names}}' |
  grep -E '^(desifaces-db|desifaces-redis|df-svc-|df-web-dev)' |
  sort
)

for C in "${LIVE[@]}"; do
  ALIAS="$(alias_for "$C")"
  [[ -n "$ALIAS" ]] || continue

  if docker inspect "$C" --format '{{json .NetworkSettings.Networks}}' | grep -q '"df-net"'; then
    CURRENT_ALIASES="$(docker inspect "$C" --format '{{with index .NetworkSettings.Networks "df-net"}}{{json .Aliases}}{{end}}')"
    if ! grep -Fq ""$ALIAS"" <<<"$CURRENT_ALIASES"; then
      echo "reset alias $C -> $ALIAS"
      docker network disconnect "$NEW_NET" "$C" >/dev/null 2>&1 || true
      docker network connect --alias "$ALIAS" "$NEW_NET" "$C"
    fi
  else
    echo "connect $C alias=$ALIAS"
    docker network connect --alias "$ALIAS" "$NEW_NET" "$C"
  fi
done

echo
echo "===== 5. RECREATE WEB ON CANONICAL NETWORK WITH CANONICAL SERVICE URLS ====="

if docker inspect "$WEB" >/dev/null 2>&1; then
  WEB_IMAGE_ID="$(docker inspect "$WEB" --format '{{.Image}}')"
  WEB_MOUNTS="$(docker inspect "$WEB" --format '{{len .Mounts}}')"
  [[ "$WEB_MOUNTS" == "0" ]] || {
    echo "FAIL: web has mounts; refusing automatic recreation"
    exit 1
  }

  ENVFILE="/tmp/df-web-dev-canonical-${STAMP}.env"
  umask 077
  docker inspect "$WEB" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$ENVFILE"

  python3 - "$ENVFILE" <<'PY'
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
    "NOTIFICATION_BASE_URL":"http://svc-core:8000",
    "ASSISTANT_BASE_URL":"http://svc-assistant:8012",
}
for k,v in updates.items():
    if k not in rows:
        order.append(k)
    rows[k]=v
p.write_text("\n".join(f"{k}={rows[k]}" for k in order)+"\n")
PY

  ROLLBACK="df-web-dev-rollback-clean-${STAMP}"
  docker stop "$WEB" >/dev/null
  docker rename "$WEB" "$ROLLBACK"

  if ! docker run -d     --name "$WEB"     --network "$NEW_NET"     --network-alias web-dev     --env-file "$ENVFILE"     -p 127.0.0.1:13000:3000     --restart unless-stopped     "$WEB_IMAGE_ID" >/dev/null
  then
    docker rename "$ROLLBACK" "$WEB"
    docker start "$WEB" >/dev/null
    echo "FAIL: canonical web recreation failed"
    exit 1
  fi

  rm -f "$ENVFILE"

  for _ in $(seq 1 45); do
    CODE="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 http://127.0.0.1:13000/auth/login 2>/dev/null || true)"
    [[ "$CODE" == "200" ]] && break
    sleep 2
  done

  [[ "$CODE" == "200" ]] || {
    docker rm -f "$WEB" >/dev/null 2>&1 || true
    docker rename "$ROLLBACK" "$WEB"
    docker start "$WEB" >/dev/null
    echo "FAIL: canonical web health failed"
    exit 1
  }

  echo "WEB_CANONICAL_RECREATE=PASS"
fi

echo
echo "===== 6. RESTART NON-STATEFUL APPLICATION CONTAINERS ====="

mapfile -t APP < <(
  docker ps --format '{{.Names}}' |
  grep -E '^(df-svc-|df-web-dev$)' |
  sort
)

for C in "${APP[@]}"; do
  echo "restart $C"
  timeout 90s docker restart -t 20 "$C" >/dev/null
done

echo
echo "===== 7. HEALTH GATE ====="

declare -A PORTS=(
  [df-svc-core]=18000
  [df-svc-fusion]=18002
  [df-svc-face]=18003
  [df-svc-audio]=18004
  [df-svc-dashboard]=18005
  [df-svc-fusion-extension]=18006
  [df-svc-commerce]=18008
  [df-svc-pricing]=18009
  [df-svc-director]=18011
  [df-svc-assistant]=18012
)

for C in "${!PORTS[@]}"; do
  docker inspect "$C" >/dev/null 2>&1 || continue
  P="${PORTS[$C]}"
  OK=0
  for _ in $(seq 1 30); do
    CODE="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 4 "http://127.0.0.1:${P}/api/health" 2>/dev/null || true)"
    if [[ "$CODE" == "200" ]]; then OK=1; break; fi
    sleep 2
  done
  (( OK == 1 )) || {
    echo "FAIL: $C health HTTP_${CODE:-NO_RESPONSE}"
    exit 1
  }
  echo "PASS $C HTTP_200"
done

echo
echo "===== 8. WEB DNS + HTTP CONTRACT ====="

timeout 30s docker exec -i "$WEB" node - <<'NODE'
const dns=require("node:dns").promises;
const targets=[
 ["CORE_BASE_URL","/api/health"],
 ["DIRECTOR_BASE_URL","/api/health"],
 ["FACE_BASE_URL","/api/health"],
 ["AUDIO_BASE_URL","/api/health"],
 ["FUSION_BASE_URL","/api/health"],
 ["FUSION_EXTENSION_BASE_URL","/api/health"],
 ["PRICING_BASE_URL","/api/health"],
 ["COMMERCE_BASE_URL","/api/health"],
 ["DASHBOARD_BASE_URL","/api/health"],
];
(async()=>{
  for(const [key,path] of targets){
    const raw=(process.env[key]||"").replace(/\/+$/,"");
    if(!raw) throw new Error(key+"_MISSING");
    const host=new URL(raw).hostname;
    const found=await dns.lookup(host);
    const r=await fetch(raw+path);
    console.log(key+" host="+host+" ip="+found.address+" HTTP_"+r.status);
    if(r.status!==200) process.exit(2);
  }
})().catch(e=>{console.error(e);process.exit(3)});
NODE

echo
echo "===== 9. DISCONNECT LEGACY NETWORK ====="

if docker network inspect "$OLD_NET" >/dev/null 2>&1; then
  mapfile -t IDS < <(
    docker network inspect "$OLD_NET" --format '{{range $id,$v := .Containers}}{{println $id}}{{end}}'
  )
  for ID in "${IDS[@]}"; do
    [[ -n "$ID" ]] || continue
    docker network disconnect -f "$OLD_NET" "$ID" >/dev/null 2>&1 || true
  done
  docker network rm "$OLD_NET" >/dev/null
  echo "LEGACY_NETWORK_REMOVED=$OLD_NET"
fi

echo
echo "===== 10. RENAME STOPPED LEGACY CONTAINERS ====="

while read -r OLD; do
  [[ -n "$OLD" ]] || continue
  STATUS="$(docker inspect "$OLD" --format '{{.State.Status}}' 2>/dev/null || true)"
  [[ "$STATUS" != "running" ]] || {
    echo "FAIL: running v3-named container remains: $OLD"
    exit 1
  }
  NEW="${OLD//v3/legacy}"
  if docker inspect "$NEW" >/dev/null 2>&1; then
    NEW="${NEW}-${STAMP}"
  fi
  echo "rename stopped $OLD -> $NEW"
  docker rename "$OLD" "$NEW"
done < <(docker ps -a --format '{{.Names}}' | grep -i 'v3' || true)

echo
echo "===== 11. FINAL ZERO-V3 RUNTIME AUDIT ====="

BAD_CONTAINERS="$(docker ps -a --format '{{.Names}}' | grep -i 'v3' || true)"
BAD_NETWORKS="$(docker network ls --format '{{.Name}}' | grep -i 'v3' || true)"
BAD_SERVICES="$(
  for C in $(docker ps --format '{{.Names}}'); do
    S="$(docker inspect "$C" --format '{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null || true)"
    if grep -qi 'v3' <<<"$S"; then echo "$C service=$S"; fi
  done
)"

[[ -z "$BAD_CONTAINERS" ]] || {
  echo "FAIL: v3 container names remain"
  echo "$BAD_CONTAINERS"
  exit 1
}
[[ -z "$BAD_NETWORKS" ]] || {
  echo "FAIL: v3 network names remain"
  echo "$BAD_NETWORKS"
  exit 1
}
[[ -z "$BAD_SERVICES" ]] || {
  echo "FAIL: v3 compose service labels remain"
  echo "$BAD_SERVICES"
  exit 1
}

echo "ZERO_V3_CONTAINER_NAMES=PASS"
echo "ZERO_V3_SERVICE_NAMES=PASS"
echo "ZERO_V3_NETWORK_NAMES=PASS"

echo
echo "===== FINAL RUNTIME ====="
docker ps --format 'table {{.Names}}	{{.Image}}	{{.Status}}' | sort

echo
echo "============================================================"
echo " DESIFACES_DEV_CLEAN_RUNTIME_START=PASS"
echo " canonical_network=df-net"
echo " production=UNTOUCHED"
echo " data_volumes=UNTOUCHED"
echo " database_container=PRESERVED"
echo " redis_container=PRESERVED"
echo "============================================================"
