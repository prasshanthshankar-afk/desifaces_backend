#!/usr/bin/env bash
set -Eeuo pipefail

WEB_REPO="prasshanthshankar-afk/desifaces_web"
TARGET_SHA="49b84b28d27fa2bb876a77bd5cb5f6995bfdac75"
TARGET_IMAGE="desifaces-web-production:${TARGET_SHA}"
CONTAINER="df-v3-web-prod"
HOST_PORT="13000"
CANDIDATE_PORT="13002"
TMP="/tmp/desifaces-web-${TARGET_SHA}"
ENV_FILE="/tmp/${CONTAINER}.env"
BUILD_LOG="/tmp/${CONTAINER}-build.log"
ROLLBACK="${CONTAINER}-rollback-20260913"

fail(){ echo "FAIL: $*" >&2; exit 1; }
cleanup(){ rm -rf "$TMP" "$ENV_FILE"; }
trap cleanup EXIT

[[ "$(hostname -s)" == "desifaces-gpu" ]] || fail "production hostname mismatch"
[[ "$(docker info --format '{{.DockerRootDir}}')" == "/var/lib/docker" ]] || fail "Docker root mismatch"
command -v git >/dev/null 2>&1 || fail "git missing on production VM"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "$CONTAINER missing"
[[ "$(docker inspect -f '{{.State.Status}}' "$CONTAINER")" == "running" ]] || fail "$CONTAINER not running"
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")" == "unless-stopped" ]] || fail "unexpected restart policy"
[[ "$(docker port "$CONTAINER" 3000/tcp 2>/dev/null)" == "127.0.0.1:${HOST_PORT}" ]] || fail "unexpected production port binding"
[[ "$(docker inspect -f '{{len .Mounts}}' "$CONTAINER")" == "0" ]] || fail "unexpected mounts on production Web container"

mapfile -t NETWORKS < <(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$CONTAINER" | sed '/^$/d')
[[ "${#NETWORKS[@]}" == "1" ]] || fail "expected exactly one Web network, found ${#NETWORKS[@]}"
NETWORK="${NETWORKS[0]}"
docker network inspect "$NETWORK" >/dev/null 2>&1 || fail "derived Web network missing: $NETWORK"

python3 - "$CONTAINER" "$ENV_FILE" <<'PY'
import json, os, subprocess, sys
c, out = sys.argv[1:]
data = json.loads(subprocess.check_output(["docker","inspect",c]))[0]["Config"].get("Env") or []
for v in data:
    if "\n" in v or "\r" in v:
        raise SystemExit("container env contains newline; refusing")
with open(out,"w",encoding="utf-8") as f:
    for v in data:
        f.write(v+"\n")
os.chmod(out,0o600)
PY

echo "============================================================"
echo " desifaces — PROD WEB HOTFIX DEPLOY V3"
echo " source=PRIVATE_GITHUB_VIA_GIT_FALLBACK"
echo " target_sha=$TARGET_SHA"
echo " derived_network=$NETWORK"
echo " scope=WEB_ONLY"
echo " backend_touch=NONE"
echo " db_touch=NONE"
echo " stripe_touch=NONE"
echo "============================================================"

echo "===== 1. FETCH EXACT PRIVATE WEB SOURCE ====="
rm -rf "$TMP"

FETCHED=0
SOURCE_MODE=""
for LOCAL in \
  /home/azureuser/workspace/desifaces-web \
  /home/azureuser/workspace/desifaces_web \
  /home/azureuser/workspace/web
 do
  if [[ -d "$LOCAL/.git" ]]; then
    URL="$(git -C "$LOCAL" remote get-url origin 2>/dev/null || true)"
    if [[ "$URL" == *"desifaces_web"* ]]; then
      if GIT_TERMINAL_PROMPT=0 git -C "$LOCAL" fetch --quiet origin "$TARGET_SHA" --depth=1 2>/dev/null; then
        git clone --quiet --no-hardlinks "$LOCAL" "$TMP"
        git -C "$TMP" checkout --quiet --detach "$TARGET_SHA"
        FETCHED=1
        SOURCE_MODE="LOCAL_AUTHENTICATED_REPO"
        break
      fi
    fi
  fi
done

if [[ "$FETCHED" == "0" ]]; then
  if GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10' \
     git clone --quiet --no-checkout --filter=blob:none "git@github.com:${WEB_REPO}.git" "$TMP" 2>/dev/null; then
    if GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10' \
       git -C "$TMP" fetch --quiet origin "$TARGET_SHA" --depth=1 2>/dev/null; then
      git -C "$TMP" checkout --quiet --detach "$TARGET_SHA"
      FETCHED=1
      SOURCE_MODE="GIT_SSH"
    fi
  fi
fi

if [[ "$FETCHED" == "0" ]]; then
  rm -rf "$TMP"
  if GIT_TERMINAL_PROMPT=0 git clone --quiet --no-checkout --filter=blob:none "https://github.com/${WEB_REPO}.git" "$TMP" 2>/dev/null; then
    if GIT_TERMINAL_PROMPT=0 git -C "$TMP" fetch --quiet origin "$TARGET_SHA" --depth=1 2>/dev/null; then
      git -C "$TMP" checkout --quiet --detach "$TARGET_SHA"
      FETCHED=1
      SOURCE_MODE="GIT_HTTPS_CREDENTIAL_HELPER"
    fi
  fi
fi

[[ "$FETCHED" == "1" ]] || fail "private desifaces_web source is not accessible via local repo, SSH, or HTTPS credentials; no production mutation performed"
[[ "$(git -C "$TMP" rev-parse HEAD)" == "$TARGET_SHA" ]] || fail "target Web SHA mismatch"
grep -Fq 'Coming Soon' "$TMP/web/components/ProductShell.tsx" || fail "Developers Coming Soon source gate missing"
grep -Fq 'object-position:center center!important' "$TMP/web/app/workspace-shell-fix.css" || fail "logo alignment source gate missing"
echo "source_mode=$SOURCE_MODE"
echo "SOURCE_GATE=PASS"

echo "===== 2. BUILD + IN-IMAGE CERTIFICATION ====="
if ! docker build -t "$TARGET_IMAGE" "$TMP/web" >"$BUILD_LOG" 2>&1; then
  tail -n 120 "$BUILD_LOG" >&2
  fail "Web image build/certification failed"
fi
echo "WEB_IMAGE_BUILD=PASS"

echo "===== 3. CANDIDATE SMOKE ====="
docker rm -f df-v3-web-prod-candidate >/dev/null 2>&1 || true
docker run -d --rm \
  --name df-v3-web-prod-candidate \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -p "127.0.0.1:${CANDIDATE_PORT}:3000" \
  "$TARGET_IMAGE" >/dev/null

CANDIDATE=000
for _ in $(seq 1 30); do
  CANDIDATE="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${CANDIDATE_PORT}/auth/login" || true)"
  [[ "$CANDIDATE" == "200" ]] && break
  sleep 2
done
if [[ "$CANDIDATE" != "200" ]]; then
  docker logs --tail 80 df-v3-web-prod-candidate >&2 || true
  docker rm -f df-v3-web-prod-candidate >/dev/null 2>&1 || true
  fail "candidate Web not healthy"
fi
docker rm -f df-v3-web-prod-candidate >/dev/null
echo "CANDIDATE_WEB=PASS"

echo "===== 4. GUARDED CUTOVER ====="
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

if ! docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -p "127.0.0.1:${HOST_PORT}:3000" \
  "$TARGET_IMAGE" >/dev/null; then
  rollback
  fail "new Web container failed to start"
fi

LOCAL=000
for _ in $(seq 1 30); do
  LOCAL="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}/auth/login" || true)"
  [[ "$LOCAL" == "200" ]] && break
  sleep 2
done
if [[ "$LOCAL" != "200" ]]; then
  docker logs --tail 80 "$CONTAINER" >&2 || true
  rollback
  fail "local Web health failed after cutover"
fi

PUBLIC="$(curl -ksS -o /dev/null -w '%{http_code}' https://web.desifaces.ai/auth/login || true)"
if [[ "$PUBLIC" != "200" ]]; then
  rollback
  fail "public Web health failed after cutover"
fi

NEW_ID="$(docker inspect -f '{{.Id}}' "$CONTAINER")"
[[ "$NEW_ID" != "$OLD_ID" ]] || fail "Web container identity did not change"
[[ "$(docker port "$CONTAINER" 3000/tcp 2>/dev/null)" == "127.0.0.1:${HOST_PORT}" ]] || { rollback; fail "new Web port binding mismatch"; }
NEW_NETS="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$CONTAINER" | sed '/^$/d')"
[[ "$NEW_NETS" == "$NETWORK" ]] || { rollback; fail "new Web network mismatch: $NEW_NETS"; }

echo "local_web=$LOCAL"
echo "public_web=$PUBLIC"
echo "active_image=$TARGET_IMAGE"
echo "active_network=$NETWORK"
echo "rollback_container=$ROLLBACK"
echo "old_image=$OLD_IMAGE"
echo "LOGO_ALIGNMENT_FIX=DEPLOYED"
echo "DEVELOPERS_COMING_SOON=DEPLOYED"
echo "PROD_WEB_HOTFIX=PASS"
echo "BACKEND_TOUCH=NONE"
echo "DB_TOUCH=NONE"
echo "STRIPE_TOUCH=NONE"
