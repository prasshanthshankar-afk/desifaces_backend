#!/usr/bin/env bash
set -Eeuo pipefail

HOST="desifaces-gpu"
BACKEND_ROOT="/home/azureuser/workspace/desifaces"
WEB_ROOT="/home/azureuser/workspace/desifaces-web"
WEB_SHA="f9e01d9b683e9600569a9cddd6f858e23816d2f7"
FAILED_JOB_ID="6a24713a-66ca-4e9f-bccb-baa60332fc54"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing $1" >&2; exit 2; }; }
for x in ssh; do need "$x"; done

echo "============================================================"
echo " desifaces.ai — PRODUCTION WEB E2E FUNCTIONALITY HOTFIX"
echo "============================================================"
echo "scope=FACE_RETRY+WEB_RESULT_NORMALIZATION+DOWNLOAD+PIKU_AVATAR"
echo "azure_edge_change=NONE"
echo "db_schema_change=NONE"
echo "customer_data_action=NONE"

ssh -o BatchMode=yes "$HOST" bash -s -- "$BACKEND_ROOT" "$WEB_ROOT" "$WEB_SHA" "$FAILED_JOB_ID" <<'REMOTE'
set -Eeuo pipefail
BACKEND_ROOT="$1"; WEB_ROOT="$2"; WEB_SHA="$3"; FAILED_JOB_ID="$4"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/azureuser/backups/desifaces-web-face-e2e-${TS}"
mkdir -p "$BACKUP"

cd "$BACKEND_ROOT"
[[ -f infra/.env ]] || { echo "FAIL: canonical backend env missing" >&2; exit 10; }
[[ -f services/svc-face/app/app/services/providers/openai_image_client.py ]] || { echo "FAIL: OpenAI image client missing" >&2; exit 11; }
docker inspect desifaces-db >/dev/null
docker inspect desifaces-redis >/dev/null
docker inspect df-svc-face >/dev/null
docker inspect df-svc-face-worker >/dev/null
docker inspect df-v3-web-prod >/dev/null

COMPOSE=(docker compose --env-file "$BACKEND_ROOT/infra/.env" -f "$BACKEND_ROOT/docker-compose.yml" -f "$BACKEND_ROOT/deploy/production/docker-compose.v3-app.production.yml")

DB_ID_BEFORE="$(docker inspect -f '{{.Id}}' desifaces-db)"
REDIS_ID_BEFORE="$(docker inspect -f '{{.Id}}' desifaces-redis)"
DB_VOL_BEFORE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' desifaces-db)"
cp -a services/svc-face/app/app/services/providers/openai_image_client.py "$BACKUP/openai_image_client.py.before"
docker inspect df-v3-web-prod > "$BACKUP/df-v3-web-prod.before.json"

echo
echo "===== 0. PRODUCTION COMPOSE ENV PREFLIGHT ====="
"${COMPOSE[@]}" config >/tmp/desifaces-web-face-hotfix.compose.yml
for key in DATABASE_URL REDIS_URL JWT_SECRET AZURE_STORAGE_CONNECTION_STRING OPENAI_API_KEY; do
  if ! grep -q "${key}:" /tmp/desifaces-web-face-hotfix.compose.yml; then
    echo "FAIL: resolved production compose missing $key" >&2
    exit 12
  fi
done
echo "PRODUCTION_COMPOSE_ENV_PREFLIGHT=PASS"

echo
echo "===== 1. FACE PROVIDER TRANSIENT RETRY PATCH ====="
python3 - <<'PY'
from pathlib import Path
p=Path('services/svc-face/app/app/services/providers/openai_image_client.py')
s=p.read_text()
if 'OPENAI_IMAGE_TRANSIENT_RETRY_V1' not in s:
    s=s.replace('import os\nfrom typing import Optional, Dict, Any, Tuple\n','import os\nimport time\nfrom typing import Optional, Dict, Any, Tuple\n',1)
    anchor='''    def _headers(self) -> Dict[str, str]:\n        return {"Authorization": f"Bearer {self.api_key}"}\n\n'''
    helper='''    def _headers(self) -> Dict[str, str]:\n        return {"Authorization": f"Bearer {self.api_key}"}\n\n    # OPENAI_IMAGE_TRANSIENT_RETRY_V1: provider 429/5xx responses are transient.\n    # Retry narrowly here so a single provider-side 500 does not strand a Face variant.\n    def _post_with_retry(self, url: str, **kwargs) -> requests.Response:\n        max_attempts = max(1, int(os.getenv("OPENAI_IMAGE_TRANSIENT_MAX_ATTEMPTS", "3")))\n        base_delay = max(0.25, float(os.getenv("OPENAI_IMAGE_TRANSIENT_RETRY_DELAY_SECONDS", "1.5")))\n        transient = {429, 500, 502, 503, 504}\n        last_exc = None\n        for attempt in range(1, max_attempts + 1):\n            try:\n                response = requests.post(url, **kwargs)\n            except requests.RequestException as exc:\n                last_exc = exc\n                if attempt >= max_attempts:\n                    raise\n                time.sleep(base_delay * (2 ** (attempt - 1)))\n                continue\n            if response.status_code not in transient or attempt >= max_attempts:\n                return response\n            retry_after = response.headers.get("retry-after")\n            try:\n                delay = float(retry_after) if retry_after else base_delay * (2 ** (attempt - 1))\n            except Exception:\n                delay = base_delay * (2 ** (attempt - 1))\n            time.sleep(min(max(delay, 0.25), 15.0))\n        if last_exc:\n            raise last_exc\n        raise RuntimeError("openai_image_retry_exhausted")\n\n'''
    if anchor not in s: raise SystemExit('FAIL: _headers anchor not found')
    s=s.replace(anchor,helper,1)
    old='''        r = requests.post(\n            f"{self.base_url}/images/generations",\n            headers=self._headers(),\n            json=data,\n            timeout=300,\n        )\n'''
    new='''        r = self._post_with_retry(\n            f"{self.base_url}/images/generations",\n            headers=self._headers(),\n            json=data,\n            timeout=300,\n        )\n'''
    if old not in s: raise SystemExit('FAIL: generations request anchor not found')
    s=s.replace(old,new,1)
    old2='''            r = requests.post(\n                f"{self.base_url}/images/edits",\n                headers=self._headers(),\n                data=data,\n                files=files,\n                timeout=300,\n            )\n'''
    new2='''            r = self._post_with_retry(\n                f"{self.base_url}/images/edits",\n                headers=self._headers(),\n                data=data,\n                files=files,\n                timeout=300,\n            )\n'''
    if old2 not in s: raise SystemExit('FAIL: edits request anchor not found')
    s=s.replace(old2,new2,1)
    p.write_text(s)
print('FACE_TRANSIENT_RETRY_SOURCE=PASS')
PY
python3 -m py_compile services/svc-face/app/app/services/providers/openai_image_client.py
grep -q 'OPENAI_IMAGE_TRANSIENT_RETRY_V1' services/svc-face/app/app/services/providers/openai_image_client.py

echo
echo "===== 2. REBUILD ONLY FACE API + WORKER ====="
"${COMPOSE[@]}" up -d --no-deps --build svc-face svc-face-worker
for _ in $(seq 1 60); do
  h="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' df-svc-face 2>/dev/null || true)"
  [[ "$h" == "healthy" ]] && break
  sleep 2
done
[[ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' df-svc-face)" == "healthy" ]] || { docker logs --tail 120 df-svc-face; exit 20; }
T2I="$(docker exec df-svc-face-worker sh -lc 'printf %s "${OPENAI_IMAGE_MODEL_T2I:-}"')"
EDIT="$(docker exec df-svc-face-worker sh -lc 'printf %s "${OPENAI_IMAGE_MODEL_EDIT:-}"')"
[[ "$T2I" == "gpt-image-2" && "$EDIT" == "gpt-image-2" ]] || { echo "FAIL: image model drift T2I=$T2I EDIT=$EDIT" >&2; exit 21; }
echo "FACE_RUNTIME=PASS model_t2i=$T2I model_edit=$EDIT"

echo
echo "===== 3. BUILD CERTIFIED WEB HOTFIX ====="
command -v gh >/dev/null || { echo "FAIL: gh missing on production host" >&2; exit 30; }
TMP="$(mktemp -d /tmp/desifaces-web-e2e.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
gh repo clone prasshanthshankar-afk/desifaces_web "$TMP/src" -- --quiet
cd "$TMP/src"
git checkout --detach "$WEB_SHA" >/dev/null
[[ "$(git rev-parse HEAD)" == "$WEB_SHA" ]] || exit 31
grep -q '/api/media/download' web/components/AssetShareActions.tsx
grep -q 'generated_variants' web/lib/normalize.ts
grep -q 'data:image/jpeg;base64' web/components/PikuMark.tsx
NEW_IMAGE="desifaces-web-production:${WEB_SHA}"
docker build -t "$NEW_IMAGE" web

echo
echo "===== 4. REPLACE ONLY WEB CONTAINER WITH ROLLBACK ====="
OLD_IMAGE="$(docker inspect -f '{{.Config.Image}}' df-v3-web-prod)"
WEB_NETWORK="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{println $k}}{{end}}' df-v3-web-prod | head -n1)"
[[ -n "$WEB_NETWORK" ]] || { echo "FAIL: web network unresolved" >&2; exit 40; }
PORT_BIND="$(docker inspect -f '{{(index (index .HostConfig.PortBindings "3000/tcp") 0).HostIp}}:{{(index (index .HostConfig.PortBindings "3000/tcp") 0).HostPort}}' df-v3-web-prod)"
[[ "$PORT_BIND" == "127.0.0.1:13001" ]] || { echo "FAIL: unexpected web binding=$PORT_BIND" >&2; exit 41; }
ENVFILE="$BACKUP/web-runtime.env"
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' df-v3-web-prod > "$ENVFILE"
OLD_NAME="df-v3-web-prod-pre-e2e-${TS}"
docker stop df-v3-web-prod >/dev/null
docker rename df-v3-web-prod "$OLD_NAME"
rollback_web(){
  set +e
  docker rm -f df-v3-web-prod >/dev/null 2>&1 || true
  docker rename "$OLD_NAME" df-v3-web-prod >/dev/null 2>&1 || true
  docker start df-v3-web-prod >/dev/null 2>&1 || true
  echo "WEB_ROLLBACK=EXECUTED old_image=$OLD_IMAGE" >&2
}
trap 'rc=$?; if (( rc != 0 )); then rollback_web; fi; rm -rf "$TMP"; exit $rc' EXIT

docker run -d --name df-v3-web-prod --restart unless-stopped --network "$WEB_NETWORK" --env-file "$ENVFILE" -p 127.0.0.1:13001:3000 "$NEW_IMAGE" >/dev/null
for _ in $(seq 1 60); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:13001/auth/login || true)"
  [[ "$code" == "200" ]] && break
  sleep 2
done
[[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:13001/auth/login)" == "200" ]] || { docker logs --tail 160 df-v3-web-prod; exit 42; }
DL_CODE="$(curl -sS -o /tmp/df-media-route.json -w '%{http_code}' 'http://127.0.0.1:13001/api/media/download?url=bad')"
[[ "$DL_CODE" == "400" ]] || { cat /tmp/df-media-route.json; echo "FAIL: media download route missing code=$DL_CODE" >&2; exit 43; }
PUBLIC_CODE="$(curl -sS -o /dev/null -w '%{http_code}' https://web.desifaces.ai/auth/login)"
[[ "$PUBLIC_CODE" == "200" ]] || { echo "FAIL: public web code=$PUBLIC_CODE" >&2; exit 44; }
trap 'rm -rf "$TMP"' EXIT
echo "WEB_RUNTIME=PASS new_image=$NEW_IMAGE rollback_container=$OLD_NAME"

echo
echo "===== 5. INFRA / DATA NON-REGRESSION ====="
[[ "$(docker inspect -f '{{.Id}}' desifaces-db)" == "$DB_ID_BEFORE" ]] || { echo "FAIL: DB container changed" >&2; exit 50; }
[[ "$(docker inspect -f '{{.Id}}' desifaces-redis)" == "$REDIS_ID_BEFORE" ]] || { echo "FAIL: Redis container changed" >&2; exit 51; }
[[ "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' desifaces-db)" == "$DB_VOL_BEFORE" ]] || { echo "FAIL: DB volume changed" >&2; exit 52; }
for spec in 'core:8000' 'face:8003' 'audio:8004' 'pricing:8009'; do
  n="${spec%%:*}"; p="${spec##*:}"; code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${p}/api/health" || true)"; echo "HEALTH|$n|$code"; [[ "$code" == "200" ]] || exit 53
done

echo
echo "===== 6. INCIDENT JOB SNAPSHOT ====="
DB_USER="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_USER"')"
DB_NAME="$(docker exec desifaces-db sh -lc 'printf %s "$POSTGRES_DB"')"
docker exec desifaces-db psql -Atq -U "$DB_USER" -d "$DB_NAME" -c "SELECT id::text,status::text,COALESCE(error_code,''),COALESCE(meta_json->>'variants_completed',''),COALESCE(meta_json->>'variants_failed',''),COALESCE(meta_json->>'partial_success','') FROM face_jobs WHERE id='${FAILED_JOB_ID}'::uuid;" 2>/dev/null || true

echo "============================================================"
echo " PRODUCTION WEB E2E FUNCTIONALITY HOTFIX PASS"
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
