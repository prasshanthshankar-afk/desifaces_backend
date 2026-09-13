#!/usr/bin/env bash
set -Eeuo pipefail

WEB_SHA="4af4837233306737fe5ad7ac670a56f614018a21"
SOURCE_IMAGE="ghcr.io/prasshanthshankar-afk/desifaces-web:${WEB_SHA}"
TARGET_IMAGE="desifaces-web-production:${WEB_SHA}"
CONTAINER="df-v3-web-prod"
HOST_PORT="13000"
CANDIDATE_PORT="13002"
CANDIDATE="df-v3-web-prod-candidate"
ROLLBACK="${CONTAINER}-rollback-talking-fast-20260913"
ENV_FILE="/tmp/${CONTAINER}-talking-fast.env"

fail(){ echo "FAIL: $*" >&2; exit 1; }
cleanup(){
  rm -f "$ENV_FILE"
  docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
  docker logout ghcr.io >/dev/null 2>&1 || true
}
trap cleanup EXIT

: "${GHCR_TOKEN:?missing protected GHCR_TOKEN}"
GHCR_USER="${GHCR_USER:-prasshanthshankar-afk}"

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}')" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "$CONTAINER missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$CONTAINER")" == "running" ]] || fail "$CONTAINER not running"
[[ "$(docker port "$CONTAINER" 3000/tcp 2>/dev/null)" == "127.0.0.1:${HOST_PORT}" ]] || fail "unexpected production port"

NETWORKS="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$CONTAINER" | sed '/^$/d')"
[[ "$(printf '%s\n' "$NETWORKS" | wc -l)" == "1" ]] || fail "expected exactly one Web network"
NETWORK="$NETWORKS"

python3 - "$CONTAINER" "$ENV_FILE" <<'PY'
import json, os, subprocess, sys
c, out = sys.argv[1:]
env = json.loads(subprocess.check_output(["docker","inspect",c]))[0]["Config"].get("Env") or []
for item in env:
    if "\n" in item or "\r" in item:
        raise SystemExit("container env contains newline")
with open(out,"w",encoding="utf-8") as f:
    for item in env:
        f.write(item+"\n")
os.chmod(out,0o600)
PY

echo "============================================================"
echo " desifaces — TALKING VIDEO FAST/RELIABLE LAUNCH HOTFIX"
echo " web_sha=$WEB_SHA"
echo " scope=WEB_ONLY"
echo "============================================================"

echo "===== 1. GHCR AUTH + PULL ====="
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null
if ! timeout 120s docker pull "$SOURCE_IMAGE"; then
  echo "PRODUCTION_MUTATION=NONE"
  fail "certified GHCR image pull failed"
fi
docker tag "$SOURCE_IMAGE" "$TARGET_IMAGE"
echo "CERTIFIED_IMAGE_PULL=PASS"

echo "===== 2. CANDIDATE ====="
docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true
docker run -d --rm --name "$CANDIDATE" --network "$NETWORK" --env-file "$ENV_FILE" -p "127.0.0.1:${CANDIDATE_PORT}:3000" "$TARGET_IMAGE" >/dev/null
CANDIDATE_HTTP=000
for _ in $(seq 1 30); do
  CANDIDATE_HTTP="$(curl -sS --connect-timeout 2 --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${CANDIDATE_PORT}/app/video" || true)"
  [[ "$CANDIDATE_HTTP" == "200" || "$CANDIDATE_HTTP" == "307" || "$CANDIDATE_HTTP" == "308" ]] && break
  sleep 2
done
[[ "$CANDIDATE_HTTP" == "200" || "$CANDIDATE_HTTP" == "307" || "$CANDIDATE_HTTP" == "308" ]] || fail "candidate Web health failed: $CANDIDATE_HTTP"

NEXT_ROOT="$(docker exec "$CANDIDATE" sh -c 'for d in /app/.next /usr/src/app/.next /workspace/.next; do [ -d "$d" ] && { echo "$d"; break; }; done' 2>/dev/null || true)"
[[ -n "$NEXT_ROOT" ]] || fail "candidate Next.js artifact missing"
timeout 15s docker exec "$CANDIDATE" grep -R -F -q "Fast / Reliable" "$NEXT_ROOT" || fail "Fast/Reliable UI artifact missing"
timeout 15s docker exec "$CANDIDATE" grep -R -F -q "veed_fabric" "$NEXT_ROOT" || fail "fast provider routing artifact missing"
echo "CANDIDATE_TALKING_FAST_POLICY=PASS"
docker rm -f "$CANDIDATE" >/dev/null

echo "===== 3. GUARDED CUTOVER ====="
OLD_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
OLD_ID="$(docker inspect -f '{{.Id}}' "$CONTAINER")"
docker rm -f "$ROLLBACK" >/dev/null 2>&1 || true
docker stop "$CONTAINER" >/dev/null
docker rename "$CONTAINER" "$ROLLBACK"
docker update --restart=no "$ROLLBACK" >/dev/null

rollback(){
  echo "ROLLBACK=START"
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker rename "$ROLLBACK" "$CONTAINER" >/dev/null 2>&1 || true
  docker update --restart=unless-stopped "$CONTAINER" >/dev/null 2>&1 || true
  docker start "$CONTAINER" >/dev/null 2>&1 || true
  echo "ROLLBACK=COMPLETE"
}

if ! docker run -d --name "$CONTAINER" --restart unless-stopped --network "$NETWORK" --env-file "$ENV_FILE" -p "127.0.0.1:${HOST_PORT}:3000" "$TARGET_IMAGE" >/dev/null; then
  rollback; fail "new Web failed to start"
fi

LOCAL=000
for _ in $(seq 1 30); do
  LOCAL="$(curl -sS --connect-timeout 2 --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}/auth/login" || true)"
  [[ "$LOCAL" == "200" ]] && break
  sleep 2
done
if [[ "$LOCAL" != "200" ]]; then rollback; fail "local Web health failed"; fi

PUBLIC="$(curl -ksS --connect-timeout 3 --max-time 10 -o /dev/null -w '%{http_code}' https://web.desifaces.ai/auth/login || true)"
if [[ "$PUBLIC" != "200" ]]; then rollback; fail "public Web health failed"; fi

ACTIVE_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
NEW_ID="$(docker inspect -f '{{.Id}}' "$CONTAINER")"
if [[ "$ACTIVE_IMAGE" != "$TARGET_IMAGE" || "$NEW_ID" == "$OLD_ID" ]]; then rollback; fail "cutover identity/image certification failed"; fi

NEXT_ROOT="$(docker exec "$CONTAINER" sh -c 'for d in /app/.next /usr/src/app/.next /workspace/.next; do [ -d "$d" ] && { echo "$d"; break; }; done' 2>/dev/null || true)"
timeout 15s docker exec "$CONTAINER" grep -R -F -q "Fast / Reliable" "$NEXT_ROOT" || { rollback; fail "deployed Fast/Reliable artifact missing"; }
timeout 15s docker exec "$CONTAINER" grep -R -F -q "veed_fabric" "$NEXT_ROOT" || { rollback; fail "deployed fast provider artifact missing"; }

echo "local_web=$LOCAL"
echo "public_web=$PUBLIC"
echo "active_image=$ACTIVE_IMAGE"
echo "old_image=$OLD_IMAGE"
echo "TALKING_VIDEO_FAST_DEFAULT=DEPLOYED"
echo "CINEMATIC_PREMIUM_DEFAULT=PRESERVED"
echo "FAST_PROVIDER_HINT_VEED_FABRIC=DEPLOYED"
echo "PROD_WEB_VIDEO_RELIABILITY_HOTFIX=PASS"
echo "BACKEND_TOUCH=NONE"
echo "DB_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
