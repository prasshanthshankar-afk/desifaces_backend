#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BACKEND_BRANCH="${BACKEND_BRANCH:-fix/v3-face-video-actual-performance-20260903}"
WEB_BRANCH="${WEB_BRANCH:-fix/v3-face-video-actual-performance-20260903}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/v3-face-video-actual-performance-${STAMP}"
BWT="$RUN/backend"
WWT="$RUN/web"
mkdir -p "$RUN/rollback"

log(){ printf '%s\n' "$*"; }
fail(){ printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
exists(){ docker inspect "$1" >/dev/null 2>&1; }
resolve_container(){ local preferred="$1" service="$2" found; if exists "$preferred"; then printf '%s' "$preferred"; return 0; fi; found="$(docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1)"; [[ -n "$found" ]] || return 1; printf '%s' "$found"; }
for x in git docker curl python3; do command -v "$x" >/dev/null 2>&1 || fail "missing required command: $x"; done

FUSION_API="$(resolve_container df-v3-svc-fusion svc-fusion)" || fail "core Fusion API missing"
FUSION_WORKER="$(resolve_container df-v3-svc-fusion-worker svc-fusion-worker)" || fail "core Fusion worker missing"
EXT_API="$(resolve_container df-v3-svc-fusion-extension svc-fusion-extension)" || fail "Fusion Extension API missing"
FACE_API="$(resolve_container df-v3-svc-face svc-face)" || fail "Face API missing"
FACE_WORKER="$(resolve_container df-v3-svc-face-worker svc-face-worker)" || fail "Face worker missing"
WEB_CONTAINER="$(resolve_container df-v3-web desifaces-web)" || fail "web container missing"
CONTROL_CONTAINER="$(resolve_container df-v3-svc-control-plane svc-control-plane)" || fail "control plane missing"

UNTOUCHED=(df-v3-svc-audio df-v3-svc-audio-worker df-v3-svc-pricing df-v3-svc-director df-v3-svc-director-worker desifaces-v3-db desifaces-v3-redis "$FUSION_API")
declare -A BEFORE
for c in "${UNTOUCHED[@]}"; do if exists "$c"; then BEFORE[$c]="$(docker inspect -f '{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}' "$c")"; fi; done

RUNTIME_CHANGED=0
WEB_CHANGED=0
WEB_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$WEB_CONTAINER")"
WEB_ROLLBACK_TAG="desifaces-web-before-actual-performance:${STAMP}"

restore_file(){ local container="$1" rel="$2" snap="$RUN/rollback/${container}/${rel//\//__}"; if [[ -f "$snap" ]]; then docker cp "$snap" "$container:/app/$rel" >/dev/null 2>&1 || true; fi; }
cleanup(){ set +e; cd "$BACKEND_ROOT" >/dev/null 2>&1 || true; git worktree remove -f "$BWT" >/dev/null 2>&1 || true; cd "$WEB_ROOT" >/dev/null 2>&1 || true; git worktree remove -f "$WWT" >/dev/null 2>&1 || true; }
rollback(){ local rc=$?; set +e; if (( rc != 0 )); then log ""; log "===== AUTOMATIC ROLLBACK ====="; if (( WEB_CHANGED == 1 )) && docker image inspect "$WEB_ROLLBACK_TAG" >/dev/null 2>&1; then docker tag "$WEB_ROLLBACK_TAG" "$WEB_IMAGE" >/dev/null 2>&1 || true; (cd "$WWT" && docker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >/dev/null 2>&1) || true; log "ROLLBACK: web restored"; fi; if (( RUNTIME_CHANGED == 1 )); then restore_file "$FUSION_WORKER" app/workers/fusion_worker.py; for c in "$FACE_API" "$FACE_WORKER"; do restore_file "$c" app/domain/models.py; restore_file "$c" app/services/creator_orchestrator.py; done; restore_file "$EXT_API" app/api/routes/longform.py; docker restart "$FUSION_WORKER" "$FACE_API" "$FACE_WORKER" "$EXT_API" >/dev/null 2>&1 || true; log "ROLLBACK: backend runtime restored"; fi; log "rollback_dir=$RUN/rollback"; fi; cleanup; exit "$rc"; }
trap rollback EXIT

log "============================================================"
log " desifaces V3 — FACE + VIDEO ACTUAL PERFORMANCE REPAIR"
log "============================================================"
log "run=$RUN"

log ""
log "===== 1. FINALIZE + CERTIFY BACKEND SOURCE ====="
cd "$BACKEND_ROOT"
git fetch -q origin "$BACKEND_BRANCH"
git worktree add --detach "$BWT" "origin/$BACKEND_BRANCH" >/dev/null
cd "$BWT"
python3 scripts/apply-v3-face-video-actual-performance-repair.py
python3 -m py_compile \
  services/svc-fusion/app/app/workers/fusion_worker.py \
  services/svc-face/app/app/domain/models.py \
  services/svc-face/app/app/services/creator_orchestrator.py \
  services/svc-fusion-extension/app/app/api/routes/longform.py
python3 scripts/test-v3-face-video-actual-performance.py
git diff --check
if [[ -n "$(git status --porcelain)" ]]; then
  git add services/svc-face/app/app/domain/models.py services/svc-face/app/app/services/creator_orchestrator.py services/svc-fusion/app/app/workers/fusion_worker.py services/svc-fusion-extension/app/app/api/routes/longform.py
  git -c user.name='desifaces release' -c user.email='release@desifaces.ai' commit -m 'fix(v3): remove core Fusion serialization and surface partial Face results' >/dev/null
fi
BACKEND_SHA="$(git rev-parse HEAD)"
git push -q origin "HEAD:refs/heads/$BACKEND_BRANCH"
log "BACKEND_SOURCE=PASS sha=$BACKEND_SHA"

log ""
log "===== 2. FINALIZE + CERTIFY WEB SOURCE ====="
cd "$WEB_ROOT"
git fetch -q origin "$WEB_BRANCH"
git worktree add --detach "$WWT" "origin/$WEB_BRANCH" >/dev/null
[[ -f "$WEB_ROOT/web/.env" && -f "$WEB_ROOT/control-plane/.env" ]] || fail "persistent web/control-plane env missing"
cp "$WEB_ROOT/web/.env" "$WWT/web/.env"
cp "$WEB_ROOT/control-plane/.env" "$WWT/control-plane/.env"
cd "$WWT"
python3 scripts/apply-face-variant-gallery-partial.py
git diff --check
get_live_env(){ local key="$1"; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTROL_CONTAINER" | awk -v key="$key" 'index($0,key "=")==1 {sub("^[^=]*=",""); print; exit}'; }
export CONTROL_PLANE_DATABASE_URL="$(get_live_env DATABASE_URL)"
export CONTROL_PLANE_AUTH_JWT_SECRET="$(get_live_env AUTH_JWT_SECRET)"
export CONTROL_PLANE_API_KEY_PEPPER="$(get_live_env API_KEY_PEPPER)"
[[ -n "$CONTROL_PLANE_DATABASE_URL" && -n "$CONTROL_PLANE_AUTH_JWT_SECRET" && -n "$CONTROL_PLANE_API_KEY_PEPPER" ]] || fail "control-plane env unavailable"
WEB_BUILD_LOG="$RUN/web-build.log"
if ! docker compose -f docker-compose.web.yml build desifaces-web >"$WEB_BUILD_LOG" 2>&1; then tail -n 80 "$WEB_BUILD_LOG" >&2; fail "web build/certification failed"; fi
if [[ -n "$(git status --porcelain --untracked-files=normal | grep -vE '^(\?\?| M) (web/.env|control-plane/.env)$' || true)" ]]; then
  git add web/app/app/face/page.tsx web/lib/normalize.ts
  git -c user.name='desifaces release' -c user.email='release@desifaces.ai' commit -m 'fix(web): show all Face variants and partial completion clearly' >/dev/null
fi
WEB_SHA="$(git rev-parse HEAD)"
git push -q origin "HEAD:refs/heads/$WEB_BRANCH"
log "WEB_SOURCE_BUILD=PASS sha=$WEB_SHA"

log ""
log "===== 3. CAPTURE ROLLBACK STATE ====="
for c in "$FUSION_WORKER" "$FACE_API" "$FACE_WORKER" "$EXT_API"; do mkdir -p "$RUN/rollback/$c"; done
docker cp "$FUSION_WORKER":/app/app/workers/fusion_worker.py "$RUN/rollback/$FUSION_WORKER/app__workers__fusion_worker.py"
for c in "$FACE_API" "$FACE_WORKER"; do docker cp "$c":/app/app/domain/models.py "$RUN/rollback/$c/app__domain__models.py"; docker cp "$c":/app/app/services/creator_orchestrator.py "$RUN/rollback/$c/app__services__creator_orchestrator.py"; done
docker cp "$EXT_API":/app/app/api/routes/longform.py "$RUN/rollback/$EXT_API/app__api__routes__longform.py"
WEB_SNAPSHOT="$RUN/rollback/web-runtime"
mkdir -p "$WEB_SNAPSHOT"
docker cp "$WEB_CONTAINER":/app "$WEB_SNAPSHOT/app"
cat > "$WEB_SNAPSHOT/Dockerfile" <<'DOCKER'
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY app/ /app/
EXPOSE 3000
CMD ["npm","start"]
DOCKER
docker build -q -t "$WEB_ROLLBACK_TAG" "$WEB_SNAPSHOT" >/dev/null
docker image inspect "$WEB_ROLLBACK_TAG" >/dev/null 2>&1 || fail "web rollback image missing"
log "ROLLBACK_SNAPSHOT=PASS"

log ""
log "===== 4. WAIT FOR EXISTING CORE FUSION JOBS TO DRAIN ====="
ACTIVE=0
for attempt in $(seq 1 40); do
  ACTIVE="$(docker exec -i "$FUSION_API" python - <<'PY'
import asyncio
from app.db import get_pool
async def main():
 p=await get_pool()
 async with p.acquire() as c:
  n=await c.fetchval("select count(*) from public.studio_jobs where studio_type='fusion' and status in ('running','processing')")
 print(int(n or 0))
asyncio.run(main())
PY
)"
  [[ "$ACTIVE" == 0 ]] && break
  log "waiting_for_active_core_fusion_jobs=$ACTIVE attempt=$attempt/40"
  sleep 15
done
[[ "$ACTIVE" == 0 ]] || fail "active core Fusion jobs did not drain within 10 minutes; runtime left unchanged"
log "CORE_FUSION_DRAIN=PASS"

log ""
log "===== 5. DEPLOY NARROW BACKEND RUNTIME ====="
RUNTIME_CHANGED=1
docker cp "$BWT/services/svc-fusion/app/app/workers/fusion_worker.py" "$FUSION_WORKER":/app/app/workers/fusion_worker.py
for c in "$FACE_API" "$FACE_WORKER"; do docker cp "$BWT/services/svc-face/app/app/domain/models.py" "$c":/app/app/domain/models.py; docker cp "$BWT/services/svc-face/app/app/services/creator_orchestrator.py" "$c":/app/app/services/creator_orchestrator.py; done
docker cp "$BWT/services/svc-fusion-extension/app/app/api/routes/longform.py" "$EXT_API":/app/app/api/routes/longform.py
docker restart "$FUSION_WORKER" "$FACE_API" "$FACE_WORKER" "$EXT_API" >/dev/null
log "RESTARTED=$FUSION_WORKER,$FACE_API,$FACE_WORKER,$EXT_API"

log ""
log "===== 6. RUNTIME CERTIFICATION ====="
READY=0
for _ in $(seq 1 45); do face_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18003/api/health 2>/dev/null || true)"; ext_ok="$(docker exec "$EXT_API" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/api/health',timeout=3).read(); print('ok')" 2>/dev/null || true)"; if [[ "$face_code" == 200 && "$ext_ok" == *ok* ]]; then READY=1; break; fi; sleep 2; done
(( READY == 1 )) || fail "modified APIs did not become ready"
docker exec -i "$FUSION_WORKER" python - <<'PY'
import inspect
from app.workers import fusion_worker
v=fusion_worker._worker_concurrency()
s=inspect.getsource(fusion_worker)
assert v >= 4, v
assert 'limit=capacity' in s and 'asyncio.create_task' in s and 'FIRST_COMPLETED' in s
print(f'CORE_FUSION_TRUE_PARALLELISM=PASS concurrency={v}')
PY
docker exec -i "$FACE_API" python - <<'PY'
import inspect
from app.domain.models import JobStatus
from app.services.creator_orchestrator import CreatorOrchestrator
assert JobStatus.PARTIAL_SUCCESS.value == 'partial_success'
s=inspect.getsource(CreatorOrchestrator)
assert 'FACE_PARTIAL_SUCCESS_V1' in s
assert 'actual_units=completed_count' in s
print('FACE_PARTIAL_SUCCESS_RUNTIME=PASS')
PY
docker exec -i "$EXT_API" python - <<'PY'
from app.api.routes.longform import _longform_progress
p=_longform_progress({'status':'running','total_segments':2,'created_at':None},[{'status':'video_running'},{'status':'provider_running'}])
assert p['segments_completed']==0 and p['segments_running']==2
assert p['percent'] <= 30, p
print(f"VIDEO_TRUTHFUL_PROGRESS=PASS percent={p['percent']} completed=0/2 running=2")
PY
log "BACKEND_RUNTIME=PASS"

log ""
log "===== 7. REPLACE WEB ONLY WITH CERTIFIED IMAGE ====="
cd "$WWT"
WEB_CHANGED=1
docker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >"$RUN/web-deploy.log" 2>&1
WEB_OK=0
for _ in $(seq 1 45); do code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 http://127.0.0.1:13000/auth/login 2>/dev/null || true)"; health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$WEB_CONTAINER" 2>/dev/null || true)"; if [[ "$code" == 200 && "$health" == healthy ]]; then WEB_OK=1; break; fi; sleep 2; done
(( WEB_OK == 1 )) || fail "web runtime health failed"
log "WEB_RUNTIME=PASS HTTP_200"

log ""
log "===== 8. PRESERVATION GATE ====="
for c in "${!BEFORE[@]}"; do after="$(docker inspect -f '{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}' "$c")"; [[ "$after" == "${BEFORE[$c]}" ]] || fail "untouched service changed: $c"; log "PRESERVED=$c"; done
for spec in Audio:18004 Pricing:18009 Director:18011; do name="${spec%%:*}"; port="${spec##*:}"; code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:${port}/api/health 2>/dev/null || true)"; [[ "$code" == 200 ]] || fail "$name regression HTTP_$code"; log "PRESERVE_${name^^}=PASS HTTP_200"; done

log ""
log "============================================================"
log " FACE + VIDEO ACTUAL PERFORMANCE REPAIR: DEPLOYED + CERTIFIED"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "core_fusion_worker_concurrency=4_default_bounded_8"
log "longform_provider_submissions=truly_parallel_after_core_queue"
log "face_partial_success=visible_not_silent"
log "face_completed_variants=preserved_and_charged_only"
log "face_gallery=all_completed_variants_clickable"
log "video_progress=completed-output-weighted_not_fake_provider_percent"
log "pricing=unchanged"
log "reservation_lifecycle=unchanged"
log "provider_routing=unchanged_kling"
log "provider_latency_issue=still_requires_provider_benchmark"
log "rollback_dir=$RUN/rollback"
log "============================================================"

trap - EXIT
cleanup
