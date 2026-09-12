#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_HOST="desifaces-dev"
BE_REMOTE="prasshanthshankar-afk/desifaces_backend"
BE_BRANCH="fix/v3-audio-read-url-refresh-20260912"
WEB_REPO="$HOME/workspace/desifaces-web"
WEB_DEV_BRANCH="fix/v3-multiperson-artifact-hydration-20260912"
WEB_REL_BRANCH="release/v3-production-web-20260912"
AUDIO_C="df-v3-svc-audio"
AUDIO_W="df-v3-svc-audio-worker"
WEB_C="df-v3-web"
ASSISTANT_C="df-v3-svc-assistant"
DIRECTOR_C="df-v3-svc-director"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE="$HOME/.local/state/audio-download-refresh-v2/$STAMP"
SRC="$STATE/source"
WEB_DEV_WT="$STATE/web-dev"
WEB_REL_WT="$STATE/web-release"
LOG="$STATE/run.log"
mkdir -p "$STATE" "$SRC"
chmod 700 "$STATE"
exec > >(tee "$LOG") 2>&1

fail(){ echo "FAIL: $*"; echo "AUDIO_DOWNLOAD_REFRESH_DEV=FAIL_CLOSED"; echo "log=$LOG"; exit 1; }

[[ "$(hostname -s)" == "$EXPECTED_HOST" ]] || fail "wrong host"
[[ -d "$WEB_REPO/.git" ]] || fail "web repo missing"
command -v gh >/dev/null || fail "gh missing"
command -v docker >/dev/null || fail "docker missing"

for c in "$AUDIO_C" "$AUDIO_W" "$WEB_C" "$ASSISTANT_C" "$DIRECTOR_C"; do
  docker inspect "$c" >/dev/null 2>&1 || fail "missing runtime $c"
  [[ "$(docker inspect -f '{{.State.Running}}' "$c")" == "true" ]] || fail "$c not running"
done

echo "============================================================"
echo " desifaces DEV — DURABLE AUDIO DOWNLOAD REFRESH V2"
echo "============================================================"
echo "environment=DEV_ONLY"
echo "database_change=NONE"
echo "pricing_change=NONE"
echo "production_touch=NONE"
echo "deployment_strategy=IN_PLACE_API_PATCH_AVOIDS_STALE_COMPOSE_LABELS"

# ---------------------------------------------------------------------------
# 1. Fetch exactly the source-controlled owner-service fix.
# ---------------------------------------------------------------------------
fetch_src(){
  local rel="$1" out="$2"
  gh api "repos/$BE_REMOTE/contents/$rel?ref=$BE_BRANCH" --jq .content | base64 -d > "$out"
}
fetch_src services/svc-audio/app/app/api/routes/canonical_audio.py "$SRC/canonical_audio.py"
fetch_src services/svc-audio/app/app/services/azure_storage_service.py "$SRC/azure_storage_service.py"

grep -q 'AzureStorageService().generate_read_url(storage_ref)' "$SRC/canonical_audio.py" || fail "fresh read-url contract missing"
grep -q 'def generate_read_url' "$SRC/azure_storage_service.py" || fail "storage signer missing"
! grep -q 'read_url = str(meta.get("source_audio_url")' "$SRC/canonical_audio.py" || fail "stale source_audio_url fallback remains"
echo "AUDIO_FRESH_SAS_SOURCE_CONTRACT=PASS"

# ---------------------------------------------------------------------------
# 2. Patch only the running Audio API writable layer and restart that container.
#    Worker, DB, Director and Assistant are intentionally untouched.
#    This is DEV runtime acceptance only; production will build from PR #19.
# ---------------------------------------------------------------------------
AUDIO_WORKER_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$AUDIO_W")"
ASSISTANT_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")"
DIRECTOR_IMAGE_BEFORE="$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")"
DB_STARTED="$(docker inspect desifaces-v3-db --format '{{.State.StartedAt}}')"

CANONICAL_PATH="$(docker exec "$AUDIO_C" python -c 'import app.api.routes.canonical_audio as m; print(m.__file__)')"
STORAGE_PATH="$(docker exec "$AUDIO_C" python -c 'import app.services.azure_storage_service as m; print(m.__file__)')"
[[ "$CANONICAL_PATH" == /* && "$STORAGE_PATH" == /* ]] || fail "unable to resolve Audio runtime source paths"

mkdir -p "$STATE/runtime-backup"
docker cp "$AUDIO_C:$CANONICAL_PATH" "$STATE/runtime-backup/canonical_audio.py"
docker cp "$AUDIO_C:$STORAGE_PATH" "$STATE/runtime-backup/azure_storage_service.py"

rollback_audio(){
  echo "AUDIO_RUNTIME_ROLLBACK=START"
  docker cp "$STATE/runtime-backup/canonical_audio.py" "$AUDIO_C:$CANONICAL_PATH" >/dev/null 2>&1 || true
  docker cp "$STATE/runtime-backup/azure_storage_service.py" "$AUDIO_C:$STORAGE_PATH" >/dev/null 2>&1 || true
  docker restart "$AUDIO_C" >/dev/null 2>&1 || true
  echo "AUDIO_RUNTIME_ROLLBACK=APPLIED"
}

# Ensure cleanup removes temporary web worktrees but does NOT undo a successful
# Audio runtime patch. Rollback is invoked explicitly on Audio readiness failure.
cleanup(){
  git -C "$WEB_REPO" worktree remove --force "$WEB_DEV_WT" >/dev/null 2>&1 || true
  git -C "$WEB_REPO" worktree remove --force "$WEB_REL_WT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker cp "$SRC/canonical_audio.py" "$AUDIO_C:$CANONICAL_PATH"
docker cp "$SRC/azure_storage_service.py" "$AUDIO_C:$STORAGE_PATH"
docker restart "$AUDIO_C" >/dev/null

ready=false
for _ in $(seq 1 30); do
  state="$(docker inspect "$AUDIO_C" --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-health{{end}}' 2>/dev/null || true)"
  if [[ "$state" == "running/healthy" || "$state" == "running/no-health" ]]; then ready=true; break; fi
  sleep 2
done
if [[ "$ready" != true ]]; then rollback_audio; fail "Audio API did not recover after runtime patch"
fi

docker exec -i "$AUDIO_C" python - <<'PY'
from app.api.routes.canonical_audio import get_audio_asset_read_url
from app.services.azure_storage_service import AzureStorageService
assert callable(get_audio_asset_read_url)
assert callable(AzureStorageService.generate_read_url)
print('AUDIO_RUNTIME_FRESH_SAS_CONTRACT=PASS')
PY
[[ "$(docker inspect -f '{{.Image}}' "$AUDIO_W")" == "$AUDIO_WORKER_IMAGE_BEFORE" ]] || fail "Audio worker image changed"
[[ "$DB_STARTED" == "$(docker inspect desifaces-v3-db --format '{{.State.StartedAt}}')" ]] || fail "DB restarted"
echo "AUDIO_RUNTIME_DEPLOY=PASS"
echo "AUDIO_WORKER_RUNTIME_PRESERVED=PASS"

# ---------------------------------------------------------------------------
# 3. Prove a fresh SAS reads a real durable Audio blob, then prove the current
#    Web download proxy transports it as non-empty media.
# ---------------------------------------------------------------------------
DB=desifaces-v3-db
PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"; PGUSER="${PGUSER:-postgres}"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"; PGDB="${PGDB:-postgres}"
STORAGE_REF="$(docker exec -i "$DB" psql -X -At -U "$PGUSER" -d "$PGDB" -c "select storage_ref from public.media_assets where kind='audio' and lifecycle_state='active' and coalesce(storage_ref,'')<>'' order by updated_at desc nulls last, created_at desc limit 1;")"
[[ -n "$STORAGE_REF" ]] || fail "no durable audio storage_ref found"

FRESH_URL="$(docker exec -e DF_STORAGE_REF="$STORAGE_REF" -i "$AUDIO_C" python - <<'PY'
import os
from app.services.azure_storage_service import AzureStorageService
print(AzureStorageService().generate_read_url(os.environ['DF_STORAGE_REF']))
PY
)"
[[ "$FRESH_URL" == https://*.blob.core.windows.net/* ]] || fail "fresh audio URL shape invalid"

DIRECT_STATUS="$(curl -sS -L -o "$STATE/audio-direct.bin" -w '%{http_code}|%{content_type}|%{size_download}' "$FRESH_URL" || true)"
IFS='|' read -r D_CODE D_TYPE D_SIZE <<< "$DIRECT_STATUS"
[[ "$D_CODE" == "200" ]] || fail "fresh Azure read failed http=$D_CODE"
python3 - "$D_SIZE" <<'PY'
import sys
assert float(sys.argv[1]) > 0
PY
echo "AUDIO_FRESH_SAS_AZURE_READ=PASS content_type=$D_TYPE bytes=$D_SIZE"

ENCODED_URL="$(python3 - "$FRESH_URL" <<'PY'
import sys,urllib.parse
print(urllib.parse.quote(sys.argv[1],safe=''))
PY
)"
PROXY_STATUS="$(curl -sS -o "$STATE/audio-proxy.bin" -w '%{http_code}|%{content_type}|%{size_download}' "http://127.0.0.1:13000/api/media/download?url=$ENCODED_URL" || true)"
IFS='|' read -r P_CODE P_TYPE P_SIZE <<< "$PROXY_STATUS"
[[ "$P_CODE" == "200" ]] || fail "web audio proxy failed http=$P_CODE"
python3 - "$P_SIZE" <<'PY'
import sys
assert float(sys.argv[1]) > 0
PY
echo "AUDIO_DOWNLOAD_PROXY=PASS content_type=$P_TYPE bytes=$P_SIZE"

# ---------------------------------------------------------------------------
# 4. Carry fresh Audio rehydration into both DEV and production Web branches.
# ---------------------------------------------------------------------------
cd "$WEB_REPO"
git fetch --quiet origin "$WEB_DEV_BRANCH" "$WEB_REL_BRANCH"
git worktree add --detach "$WEB_DEV_WT" "origin/$WEB_DEV_BRANCH" >/dev/null
git worktree add --detach "$WEB_REL_WT" "origin/$WEB_REL_BRANCH" >/dev/null

patch_web(){
  local wt="$1"
  python3 - "$wt/web/components/MultiPersonDirector.tsx" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); s=p.read_text()
s=s.replace('async function hydrateFaceStageArtifacts() {','async function hydrateStageArtifacts() {',1)
s=s.replace('const faceStages = (workflow?.stages || []).filter((stage: any) => {\n        if (stage?.stage_type !== "face") return false;','const assetStages = (workflow?.stages || []).filter((stage: any) => {\n        if (!["face", "audio"].includes(clean(stage?.stage_type))) return false;',1)
s=s.replace('if (!faceStages.length) return;','if (!assetStages.length) return;',1)
s=s.replace('faceStages.map(async (stage: any) => {','assetStages.map(async (stage: any) => {',1)
old='''            const media = await dfFetch<any>(\n              "face",\n              `/api/face/assets/${encodeURIComponent(mediaId)}/read-url`,\n            );\n            const url = clean(media?.read_url || media?.image_url || media?.url);'''
new='''            const isAudio = clean(stage?.stage_type) === "audio";\n            const media = await dfFetch<any>(\n              isAudio ? "audio" : "face",\n              isAudio\n                ? `/api/audio/assets/${encodeURIComponent(mediaId)}/read-url`\n                : `/api/face/assets/${encodeURIComponent(mediaId)}/read-url`,\n            );\n            const url = clean(media?.read_url || media?.audio_url || media?.image_url || media?.url);'''
if old in s: s=s.replace(old,new,1)
elif '/api/audio/assets/${encodeURIComponent(mediaId)}/read-url' not in s: raise SystemExit('FAIL: hydration fetch pattern not found')
s=s.replace('void hydrateFaceStageArtifacts();','void hydrateStageArtifacts();',1)
old_media='const mediaUrl = stage.stage_type === "story_final" ? (finalMediaUrl || stageMedia(stage, result)) : (stageMedia(stage, result) || stageMediaUrls[clean(stage.stage_run_id)] || "");'
new_media='const mediaUrl = stage.stage_type === "story_final" ? (finalMediaUrl || stageMedia(stage, result)) : (stageMediaUrls[clean(stage.stage_run_id)] || stageMedia(stage, result) || "");'
if old_media in s: s=s.replace(old_media,new_media,1)
elif new_media not in s: raise SystemExit('FAIL: media precedence pattern not found')
required=['hydrateStageArtifacts','["face", "audio"]','/api/audio/assets/${encodeURIComponent(mediaId)}/read-url','media?.audio_url','stageMediaUrls[clean(stage.stage_run_id)] || stageMedia(stage, result)']
for marker in required:
    if marker not in s: raise SystemExit('FAIL missing '+marker)
p.write_text(s)
PY
  git -C "$wt" diff --check
}
patch_web "$WEB_DEV_WT"
patch_web "$WEB_REL_WT"

NEW_MP_BLOB="$(git -C "$WEB_REL_WT" hash-object web/components/MultiPersonDirector.tsx)"
python3 - "$WEB_REL_WT/scripts/certify-v3-production-web-release.sh" "$NEW_MP_BLOB" <<'PY'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); new=sys.argv[2]; s=p.read_text()
s2,n=re.subn(r'EXPECTED_MULTIPERSON_BLOB="[0-9a-f]{40}"',f'EXPECTED_MULTIPERSON_BLOB="{new}"',s,count=1)
if n!=1: raise SystemExit('FAIL: production component provenance marker missing')
p.write_text(s2)
PY

commit_web(){
  local wt="$1" branch="$2" msg="$3"
  if ! git -C "$wt" diff --quiet; then
    git -C "$wt" add web/components/MultiPersonDirector.tsx
    [[ "$branch" == "$WEB_REL_BRANCH" ]] && git -C "$wt" add scripts/certify-v3-production-web-release.sh
    git -C "$wt" -c user.name='desifaces-dev' -c user.email='azureuser@desifaces-dev' commit -m "$msg" >/dev/null
    git -C "$wt" push origin "HEAD:$branch" >/dev/null
  fi
}
commit_web "$WEB_DEV_WT" "$WEB_DEV_BRANCH" "fix(web): refresh durable Audio URLs for playback and download"
commit_web "$WEB_REL_WT" "$WEB_REL_BRANCH" "fix(web): carry durable Audio URL refresh into production candidate"

DEV_HEAD="$(git -C "$WEB_DEV_WT" rev-parse HEAD)"
REL_HEAD="$(git -C "$WEB_REL_WT" rev-parse HEAD)"
DEV_BLOB="$(git -C "$WEB_DEV_WT" hash-object web/components/MultiPersonDirector.tsx)"
REL_BLOB="$(git -C "$WEB_REL_WT" hash-object web/components/MultiPersonDirector.tsx)"
[[ "$DEV_BLOB" == "$REL_BLOB" ]] || fail "web DEV/release component mismatch"
[[ "$(git -C "$WEB_DEV_WT" hash-object web/components/AssetShareActions.tsx)" == "$(git -C "$WEB_REL_WT" hash-object web/components/AssetShareActions.tsx)" ]] || fail "AssetShareActions DEV/release mismatch"
echo "WEB_AUDIO_REHYDRATION_SOURCE_CONTRACT=PASS"

# ---------------------------------------------------------------------------
# 5. Build both Web candidates and replace only DEV Web.
# ---------------------------------------------------------------------------
DEV_IMAGE="desifaces-web:audio-refresh-${DEV_HEAD:0:12}"
REL_IMAGE="desifaces-web:production-candidate-${REL_HEAD:0:12}"
docker build --label "org.opencontainers.image.revision=$DEV_HEAD" -t "$DEV_IMAGE" "$WEB_DEV_WT/web"
echo "DEV_WEB_BUILD=PASS"
docker build --label "org.opencontainers.image.revision=$REL_HEAD" -t "$REL_IMAGE" "$WEB_REL_WT/web"
echo "PRODUCTION_WEB_CANDIDATE_BUILD=PASS"

OLD_WEB_IMAGE="$(docker inspect -f '{{.Image}}' "$WEB_C")"
ROLLBACK_TAG="desifaces-web:audio-refresh-rollback-$STAMP"
docker image tag "$OLD_WEB_IMAGE" "$ROLLBACK_TAG"
ENV_OUT="$STATE/web.env"
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$WEB_C" > "$ENV_OUT"
chmod 600 "$ENV_OUT"
RESTART="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$WEB_C")"; [[ -n "$RESTART" ]] || RESTART=no
[[ "$(docker inspect -f '{{len .Mounts}}' "$WEB_C")" == "0" ]] || fail "unexpected web mounts"
mapfile -t PORT_ARGS < <(docker inspect "$WEB_C" | python3 -c '
import json,sys
x=json.load(sys.stdin)[0]
for cp,bs in (x.get("HostConfig",{}).get("PortBindings") or {}).items():
  for b in bs or []:
    hp=b.get("HostPort"); ip=b.get("HostIp") or ""
    if hp: print("--publish="+(f"{ip}:{hp}:{cp}" if ip else f"{hp}:{cp}"))
')
mapfile -t ALIASES < <(docker inspect "$WEB_C" | python3 -c '
import json,sys
x=json.load(sys.stdin)[0]; name=(x.get("Name") or "").lstrip("/")
net=(x.get("NetworkSettings",{}).get("Networks",{}).get("df-v3-net") or {})
for a in net.get("Aliases") or []:
  if a and a!=name: print(a)
')
ALIAS_ARGS=(); for a in "${ALIASES[@]}"; do ALIAS_ARGS+=(--network-alias "$a"); done
rollback_web(){
  docker rm -f "$WEB_C" >/dev/null 2>&1 || true
  docker run -d --name "$WEB_C" --restart="$RESTART" --env-file "$ENV_OUT" --network df-v3-net "${ALIAS_ARGS[@]}" "${PORT_ARGS[@]}" "$ROLLBACK_TAG" >/dev/null || true
}

docker stop -t 20 "$WEB_C" >/dev/null
docker rm "$WEB_C" >/dev/null
if ! docker run -d --name "$WEB_C" --restart="$RESTART" --env-file "$ENV_OUT" --network df-v3-net "${ALIAS_ARGS[@]}" "${PORT_ARGS[@]}" "$DEV_IMAGE" >/dev/null; then
  rollback_web; fail "DEV Web launch failed"
fi
HTTP=""
for _ in $(seq 1 45); do
  HTTP="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:13000/auth/login 2>/dev/null || true)"
  [[ "$HTTP" == "200" ]] && break
  sleep 2
done
[[ "$HTTP" == "200" ]] || { rollback_web; fail "DEV Web health failed"; }
echo "DEV_WEB_DEPLOY=PASS"

POST_PROXY="$(curl -sS -o "$STATE/audio-proxy-post-web.bin" -w '%{http_code}|%{content_type}|%{size_download}' "http://127.0.0.1:13000/api/media/download?url=$ENCODED_URL" || true)"
IFS='|' read -r PP_CODE PP_TYPE PP_SIZE <<< "$POST_PROXY"
[[ "$PP_CODE" == "200" ]] || fail "post-deploy audio proxy http=$PP_CODE"
python3 - "$PP_SIZE" <<'PY'
import sys
assert float(sys.argv[1]) > 0
PY
echo "AUDIO_DOWNLOAD_PROXY_POST_WEB=PASS content_type=$PP_TYPE bytes=$PP_SIZE"

[[ "$(docker inspect -f '{{.Image}}' "$ASSISTANT_C")" == "$ASSISTANT_IMAGE_BEFORE" ]] || fail "Assistant image changed"
[[ "$(docker inspect -f '{{.Image}}' "$DIRECTOR_C")" == "$DIRECTOR_IMAGE_BEFORE" ]] || fail "Director image changed"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18012/api/health 2>/dev/null || true)" == "200" ]] || fail "Piku health failed"
[[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18012/api/ready 2>/dev/null || true)" == "200" ]] || fail "Piku ready failed"

echo "ASSISTANT_RUNTIME_PRESERVED=PASS"
echo "DIRECTOR_RUNTIME_PRESERVED=PASS"
echo "PIKU_HEALTH_READY=PASS"

echo "============================================================"
echo " FINAL VERDICT"
echo "============================================================"
echo "AUDIO_FRESH_SAS_SOURCE_CONTRACT=PASS"
echo "AUDIO_RUNTIME_FRESH_SAS_CONTRACT=PASS"
echo "AUDIO_RUNTIME_DEPLOY=PASS"
echo "AUDIO_WORKER_RUNTIME_PRESERVED=PASS"
echo "AUDIO_FRESH_SAS_AZURE_READ=PASS"
echo "AUDIO_DOWNLOAD_PROXY=PASS"
echo "WEB_AUDIO_REHYDRATION_SOURCE_CONTRACT=PASS"
echo "DEV_WEB_BUILD=PASS"
echo "DEV_WEB_DEPLOY=PASS"
echo "AUDIO_DOWNLOAD_PROXY_POST_WEB=PASS"
echo "PRODUCTION_WEB_CANDIDATE_BUILD=PASS"
echo "DATABASE_CHANGE=NONE"
echo "PRICING_CHANGE=NONE"
echo "PRODUCTION_TOUCH=NONE"
echo "backend_branch=$BE_BRANCH"
echo "web_dev_commit=$DEV_HEAD"
echo "web_release_commit=$REL_HEAD"
echo "production_candidate_image=$REL_IMAGE"
echo "log=$LOG"
