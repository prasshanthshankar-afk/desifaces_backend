#!/usr/bin/env bash
set -Eeuo pipefail

HOST="desifaces-gpu"
WEB_SHA="f9e01d9b683e9600569a9cddd6f858e23816d2f7"
WEB_REPO="prasshanthshankar-afk/desifaces_web"
REMOTE_ARCHIVE="/tmp/desifaces-web-hotfix-${WEB_SHA}.tar.gz"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
for x in gh git ssh scp tar; do need "$x"; done

echo "============================================================"
echo " desifaces.ai — WEB FACE E2E HOTFIX V2"
echo "============================================================"
echo "face_runtime=VERIFY_EXISTING_PATCH"
echo "web_source_transfer=LOCAL_AUTHENTICATED_GITHUB_TO_GPU"
echo "db_redis_change=NONE"
echo "azure_edge_change=NONE"

TMP_LOCAL="$(mktemp -d /tmp/desifaces-web-hotfix.XXXXXX)"
cleanup(){ rm -rf "$TMP_LOCAL"; }
trap cleanup EXIT

echo
echo "===== 1. PACKAGE EXACT WEB HOTFIX ON MAC ====="
gh repo clone "$WEB_REPO" "$TMP_LOCAL/src" -- --quiet
cd "$TMP_LOCAL/src"
git checkout --detach "$WEB_SHA" >/dev/null
[[ "$(git rev-parse HEAD)" == "$WEB_SHA" ]] || { echo "FAIL: exact web SHA mismatch" >&2; exit 3; }
grep -q '/api/media/download' web/components/AssetShareActions.tsx || { echo "FAIL: same-origin download proxy wiring missing" >&2; exit 4; }
grep -q 'generated_variants' web/lib/normalize.ts || { echo "FAIL: Face result normalization hotfix missing" >&2; exit 5; }
grep -q 'data:image/jpeg;base64' web/components/PikuMark.tsx || { echo "FAIL: supplied Piku avatar missing" >&2; exit 6; }
git archive --format=tar.gz --output="$TMP_LOCAL/web-hotfix.tar.gz" "$WEB_SHA"
[[ -s "$TMP_LOCAL/web-hotfix.tar.gz" ]] || { echo "FAIL: web source archive not created" >&2; exit 7; }
echo "WEB_SOURCE_PACKAGE=PASS sha=$WEB_SHA"

echo
echo "===== 2. TRANSFER EXACT PACKAGE TO GPU ====="
scp -q "$TMP_LOCAL/web-hotfix.tar.gz" "$HOST:$REMOTE_ARCHIVE"
ssh -o BatchMode=yes "$HOST" "test -s '$REMOTE_ARCHIVE'"
echo "WEB_SOURCE_TRANSFER=PASS"

echo
echo "===== 3. GPU DEPLOY + CERTIFICATION ====="
ssh -o BatchMode=yes "$HOST" bash -s -- "$WEB_SHA" "$REMOTE_ARCHIVE" <<'REMOTE'
set -Eeuo pipefail
WEB_SHA="$1"
REMOTE_ARCHIVE="$2"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/desifaces-web-e2e-v2-${TS}"
mkdir -p "$BACKUP"

for c in desifaces-db desifaces-redis df-svc-face df-svc-face-worker df-v3-web-prod; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: required container missing: $c" >&2; exit 20; }
done

DB_ID_BEFORE="$(docker inspect -f '{{.Id}}' desifaces-db)"
REDIS_ID_BEFORE="$(docker inspect -f '{{.Id}}' desifaces-redis)"
DB_VOL_BEFORE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' desifaces-db)"

# Face patch was already successfully deployed by the prior run. Prove it before touching web.
docker exec df-svc-face-worker grep -q 'OPENAI_IMAGE_TRANSIENT_RETRY_V1' /app/app/services/providers/openai_image_client.py || { echo "FAIL: Face transient retry patch is not active" >&2; exit 21; }
T2I="$(docker exec df-svc-face-worker sh -lc 'printf %s "${OPENAI_IMAGE_MODEL_T2I:-}"')"
EDIT="$(docker exec df-svc-face-worker sh -lc 'printf %s "${OPENAI_IMAGE_MODEL_EDIT:-}"')"
[[ "$T2I" == "gpt-image-2" && "$EDIT" == "gpt-image-2" ]] || { echo "FAIL: image model drift T2I=$T2I EDIT=$EDIT" >&2; exit 22; }
FACE_HEALTH="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8003/api/health || true)"
[[ "$FACE_HEALTH" == "200" ]] || { echo "FAIL: Face API unhealthy code=$FACE_HEALTH" >&2; exit 23; }
echo "FACE_RUNTIME_ALREADY_PATCHED=PASS model_t2i=$T2I model_edit=$EDIT"

WORK="$(mktemp -d /tmp/desifaces-web-e2e-v2.XXXXXX)"
cleanup_remote(){ rm -rf "$WORK" "$REMOTE_ARCHIVE"; }
trap cleanup_remote EXIT
tar -xzf "$REMOTE_ARCHIVE" -C "$WORK"
grep -q '/api/media/download' "$WORK/web/components/AssetShareActions.tsx"
grep -q 'generated_variants' "$WORK/web/lib/normalize.ts"
grep -q 'data:image/jpeg;base64' "$WORK/web/components/PikuMark.tsx"

NEW_IMAGE="desifaces-web-production:${WEB_SHA}"
echo "WEB_IMAGE_BUILD=$NEW_IMAGE"
docker build -t "$NEW_IMAGE" "$WORK/web"

OLD_IMAGE="$(docker inspect -f '{{.Config.Image}}' df-v3-web-prod)"
WEB_NETWORK="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' df-v3-web-prod | head -n1)"
PORT_BIND="$(docker inspect -f '{{(index (index .HostConfig.PortBindings "3000/tcp") 0).HostIp}}:{{(index (index .HostConfig.PortBindings "3000/tcp") 0).HostPort}}' df-v3-web-prod)"
[[ -n "$WEB_NETWORK" ]] || { echo "FAIL: web network unresolved" >&2; exit 30; }
[[ "$PORT_BIND" == "127.0.0.1:13001" ]] || { echo "FAIL: unexpected web binding=$PORT_BIND" >&2; exit 31; }

docker inspect df-v3-web-prod > "$BACKUP/df-v3-web-prod.before.json"
ENVFILE="$BACKUP/web-runtime.env"
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' df-v3-web-prod > "$ENVFILE"
OLD_NAME="df-v3-web-prod-pre-e2e-v2-${TS}"

docker stop df-v3-web-prod >/dev/null
docker rename df-v3-web-prod "$OLD_NAME"
rollback_web(){
  set +e
  docker rm -f df-v3-web-prod >/dev/null 2>&1 || true
  docker rename "$OLD_NAME" df-v3-web-prod >/dev/null 2>&1 || true
  docker start df-v3-web-prod >/dev/null 2>&1 || true
  echo "WEB_ROLLBACK=EXECUTED old_image=$OLD_IMAGE" >&2
}
trap 'rc=$?; if (( rc != 0 )); then rollback_web; fi; cleanup_remote; exit $rc' EXIT

docker run -d \
  --name df-v3-web-prod \
  --restart unless-stopped \
  --network "$WEB_NETWORK" \
  --env-file "$ENVFILE" \
  -p 127.0.0.1:13001:3000 \
  "$NEW_IMAGE" >/dev/null

for _ in $(seq 1 60); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:13001/auth/login || true)"
  [[ "$code" == "200" ]] && break
  sleep 2
done
LOCAL_WEB="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:13001/auth/login || true)"
[[ "$LOCAL_WEB" == "200" ]] || { docker logs --tail 160 df-v3-web-prod; echo "FAIL: local web unhealthy code=$LOCAL_WEB" >&2; exit 32; }

DL_CODE="$(curl -sS -o /tmp/df-media-route.json -w '%{http_code}' 'http://127.0.0.1:13001/api/media/download?url=bad' || true)"
[[ "$DL_CODE" == "400" ]] || { cat /tmp/df-media-route.json || true; echo "FAIL: media download proxy route unavailable code=$DL_CODE" >&2; exit 33; }

PUBLIC_CODE="$(curl -sS -o /dev/null -w '%{http_code}' https://web.desifaces.ai/auth/login || true)"
[[ "$PUBLIC_CODE" == "200" ]] || { echo "FAIL: public web unavailable code=$PUBLIC_CODE" >&2; exit 34; }

# Prove exact web image and hotfix artifacts are live.
LIVE_IMAGE="$(docker inspect -f '{{.Config.Image}}' df-v3-web-prod)"
[[ "$LIVE_IMAGE" == "$NEW_IMAGE" ]] || { echo "FAIL: unexpected live web image=$LIVE_IMAGE" >&2; exit 35; }
docker run --rm --entrypoint sh "$NEW_IMAGE" -lc 'grep -Rqs "/api/media/download" /app 2>/dev/null || grep -Rqs "/api/media/download" .next 2>/dev/null' || { echo "FAIL: download proxy wiring not found in built image" >&2; exit 36; }

echo "WEB_RUNTIME=PASS new_image=$NEW_IMAGE rollback_container=$OLD_NAME"

[[ "$(docker inspect -f '{{.Id}}' desifaces-db)" == "$DB_ID_BEFORE" ]] || { echo "FAIL: DB container changed" >&2; exit 40; }
[[ "$(docker inspect -f '{{.Id}}' desifaces-redis)" == "$REDIS_ID_BEFORE" ]] || { echo "FAIL: Redis container changed" >&2; exit 41; }
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' desifaces-db)" == "$DB_VOL_BEFORE" ]] || { echo "FAIL: DB volume changed" >&2; exit 42; }

for spec in 'core:8000' 'face:8003' 'audio:8004' 'pricing:8009'; do
  n="${spec%%:*}"; p="${spec##*:}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${p}/api/health" || true)"
  echo "HEALTH|$n|$code"
  [[ "$code" == "200" ]] || exit 43
done

trap cleanup_remote EXIT
cleanup_remote

echo "============================================================"
echo " PRODUCTION WEB E2E FUNCTIONALITY HOTFIX V2 PASS"
echo "============================================================"
echo "FACE_TRANSIENT_RETRY=PASS"
echo "FACE_MODEL_GPT_IMAGE_2=PASS"
echo "WEB_FACE_RESULT_NORMALIZATION=PASS"
echo "WEB_PNG_DOWNLOAD_PROXY=PASS"
echo "PIKU_SUPPLIED_AVATAR=PASS"
echo "PUBLIC_WEB=PASS"
echo "DB_REDIS_UNCHANGED=PASS"
echo "AZURE_EDGE_CHANGE=NONE"
echo "NEXT_ACTION=USER_FACE_STUDIO_E2E_RETEST"
REMOTE
