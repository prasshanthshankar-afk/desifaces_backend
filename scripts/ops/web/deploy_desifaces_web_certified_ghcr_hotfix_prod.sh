#!/usr/bin/env bash
set -Eeuo pipefail

WEB_SHA="49b84b28d27fa2bb876a77bd5cb5f6995bfdac75"
SOURCE_IMAGE="ghcr.io/prasshanthshankar-afk/desifaces-web:${WEB_SHA}"
TARGET_IMAGE="desifaces-web-production:${WEB_SHA}"
CONTAINER="df-v3-web-prod"
HOST_PORT="13000"
CANDIDATE_PORT="13002"
ROLLBACK="${CONTAINER}-rollback-20260913"
ENV_FILE="/tmp/${CONTAINER}.env"

fail(){ echo "FAIL: $*" >&2; exit 1; }
cleanup(){ rm -f "$ENV_FILE"; docker rm -f df-v3-web-prod-candidate >/dev/null 2>&1 || true; }
trap cleanup EXIT

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}')" == "/var/lib/docker" ]] || fail "Docker root mismatch"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "$CONTAINER missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$CONTAINER")" == "running" ]] || fail "$CONTAINER not running"
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")" == "unless-stopped" ]] || fail "unexpected restart policy"
[[ "$(docker port "$CONTAINER" 3000/tcp 2>/dev/null)" == "127.0.0.1:${HOST_PORT}" ]] || fail "unexpected production port"
[[ "$(docker inspect -f '{{len .Mounts}}' "$CONTAINER")" == "0" ]] || fail "unexpected mounts"

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
echo " desifaces — CERTIFIED GHCR WEB HOTFIX DEPLOY"
echo " web_sha=$WEB_SHA"
echo " source_image=$SOURCE_IMAGE"
echo " derived_network=$NETWORK"
echo " scope=WEB_ONLY"
echo "============================================================"

echo "===== 1. PULL CERTIFIED IMAGE ====="
if ! docker pull "$SOURCE_IMAGE"; then
  echo "GHCR_PULL=FAIL"
  echo "PRODUCTION_MUTATION=NONE"
  fail "production VM cannot pull certified GHCR image"
fi
docker tag "$SOURCE_IMAGE" "$TARGET_IMAGE"
echo "GHCR_CERTIFIED_IMAGE_PULL=PASS"

echo "===== 2. CANDIDATE SMOKE ====="
docker rm -f df-v3-web-prod-candidate >/dev/null 2>&1 || true
docker run -d --rm --name df-v3-web-prod-candidate --network "$NETWORK" --env-file "$ENV_FILE" -p "127.0.0.1:${CANDIDATE_PORT}:3000" "$TARGET_IMAGE" >/dev/null
CANDIDATE=000
for _ in $(seq 1 30); do
  CANDIDATE="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${CANDIDATE_PORT}/auth/login" || true)"
  [[ "$CANDIDATE" == "200" ]] && break
  sleep 2
done
[[ "$CANDIDATE" == "200" ]] || fail "candidate Web health failed"
docker rm -f df-v3-web-prod-candidate >/dev/null
echo "CANDIDATE_WEB=PASS"

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
  LOCAL="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}/auth/login" || true)"
  [[ "$LOCAL" == "200" ]] && break
  sleep 2
done
if [[ "$LOCAL" != "200" ]]; then rollback; fail "local Web health failed"; fi

PUBLIC="$(curl -ksS -o /dev/null -w '%{http_code}' https://web.desifaces.ai/auth/login || true)"
if [[ "$PUBLIC" != "200" ]]; then rollback; fail "public Web health failed"; fi

NEW_ID="$(docker inspect -f '{{.Id}}' "$CONTAINER")"
[[ "$NEW_ID" != "$OLD_ID" ]] || fail "Web container identity did not change"
[[ "$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$CONTAINER" | sed '/^$/d')" == "$NETWORK" ]] || { rollback; fail "Web network changed"; }

echo "local_web=$LOCAL"
echo "public_web=$PUBLIC"
echo "active_image=$TARGET_IMAGE"
echo "rollback_container=$ROLLBACK"
echo "old_image=$OLD_IMAGE"
echo "LOGO_ALIGNMENT_FIX=DEPLOYED"
echo "DEVELOPERS_COMING_SOON=DEPLOYED"
echo "PROD_WEB_HOTFIX=PASS"
echo "BACKEND_TOUCH=NONE"
echo "DB_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
