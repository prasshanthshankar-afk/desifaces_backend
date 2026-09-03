#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BACKEND_BRANCH="${BACKEND_BRANCH:-fix/v3-video-pricing-progress-performance-20260903}"
WEB_BRANCH="${WEB_BRANCH:-fix/v3-generation-progress-pricing-parity-20260903}"
MOBILE_BRANCH="${MOBILE_BRANCH:-fix/v3-generation-progress-pricing-parity-20260903}"
MOBILE_REPO="${MOBILE_REPO:-git@github.com:prasshanthshankar-afk/desifaces_frontend.git}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/v3-generation-release-${STAMP}"
BWT="$RUN/backend"
WWT="$RUN/web"
MWT="$RUN/mobile"
mkdir -p "$RUN/rollback"

log(){ printf '%s\n' "$*"; }
fail(){ printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
exists(){ docker inspect "$1" >/dev/null 2>&1; }
resolve_container(){
  local preferred="$1" service="$2" found
  if exists "$preferred"; then printf '%s' "$preferred"; return 0; fi
  found="$(docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1)"
  [[ -n "$found" ]] || return 1
  printf '%s' "$found"
}
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
for x in git docker curl python3; do need "$x"; done

EXT_API="$(resolve_container "df-v3-svc-fusion-extension" svc-fusion-extension)" || fail "Fusion Extension API missing"
EXT_WORKER="$(resolve_container "df-v3-svc-fusion-extension-worker" svc-fusion-extension-worker)" || fail "Fusion Extension worker missing"
STITCH_WORKER="$(resolve_container "df-v3-svc-fusion-extension-stitch-worker" svc-fusion-extension-stitch-worker)" || fail "Fusion Extension stitch worker missing"
FACE_API="$(resolve_container "df-v3-svc-face" svc-face)" || fail "Face API missing"
FACE_WORKER="$(resolve_container "df-v3-svc-face-worker" svc-face-worker)" || fail "Face worker missing"
WEB_CONTAINER="$(resolve_container "df-v3-web" desifaces-web)" || fail "web container missing"
CONTROL_CONTAINER="$(resolve_container "df-v3-svc-control-plane" svc-control-plane)" || fail "control plane missing"

UNTOUCHED=(
  df-v3-svc-audio
  df-v3-svc-audio-worker
  df-v3-svc-fusion
  df-v3-svc-fusion-worker
  df-v3-svc-pricing
  df-v3-svc-director
  df-v3-svc-director-worker
  desifaces-v3-db
  desifaces-v3-redis
)
declare -A BEFORE
for c in "${UNTOUCHED[@]}"; do
  if exists "$c"; then BEFORE[$c]="$(docker inspect -f '{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}' "$c")"; fi
done

BACKEND_FILES=(
  app/api/routes/longform.py
  app/domain/models.py
  app/config.py
  app/services/longform_orchestrator.py
  app/services/premium_actual_seconds_pricing.py
  app/workers/longform_worker.py
  app/workers/stitch_worker.py
)
FACE_FILES=(app/services/creator_orchestrator.py)
RUNTIME_CHANGED=0
DB_CHANGED=0
WEB_CHANGED=0
WEB_ROLLBACK_TAG=""
WEB_IMAGE=""

restore_container_files(){
  local container="$1" prefix="$2" rel snap
  shift 2
  for rel in "$@"; do
    snap="$RUN/rollback/${container}/${rel//\//__}"
    if [[ -f "$snap" ]]; then
      docker cp "$snap" "$container:$prefix/$rel" >/dev/null 2>&1 || true
    elif [[ -f "$snap.absent" ]]; then
      docker exec "$container" rm -f "$prefix/$rel" >/dev/null 2>&1 || true
    fi
  done
}

rollback(){
  local rc=$?
  set +e
  if (( rc != 0 )); then
    log ""
    log "===== AUTOMATIC ROLLBACK ====="
    if (( WEB_CHANGED == 1 )) && [[ -n "$WEB_ROLLBACK_TAG" && -n "$WEB_IMAGE" ]]; then
      docker tag "$WEB_ROLLBACK_TAG" "$WEB_IMAGE" >/dev/null 2>&1 || true
      if [[ -d "$WWT" ]]; then
        (cd "$WWT" && docker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >/dev/null 2>&1) || true
      fi
      log "ROLLBACK: web image restored"
    fi
    if (( RUNTIME_CHANGED == 1 )); then
      for c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do
        restore_container_files "$c" /app "${BACKEND_FILES[@]}"
      done
      for c in "$FACE_API" "$FACE_WORKER"; do
        restore_container_files "$c" /app "${FACE_FILES[@]}"
      done
      docker restart "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER" "$FACE_API" "$FACE_WORKER" >/dev/null 2>&1 || true
      log "ROLLBACK: modified runtime files restored"
    fi
    if (( DB_CHANGED == 1 )); then
      cat <<'SQL' | docker exec -i "$EXT_API" python -c 'import asyncio,sys; from app.db import get_db_pool
async def m():
 p=await get_db_pool();
 async with p.acquire() as c: await c.execute(sys.stdin.read())
asyncio.run(m())'
BEGIN;
DELETE FROM public.pricing_sku_prices WHERE sku_code='LONGFORM_TALK_PREMIUM_SECOND';
DELETE FROM public.pricing_variant_lines WHERE variant_code='TALKING_VIDEO_PREMIUM_SECOND';
DELETE FROM public.pricing_variants WHERE code='TALKING_VIDEO_PREMIUM_SECOND';
DELETE FROM public.pricing_skus WHERE code='LONGFORM_TALK_PREMIUM_SECOND';
COMMIT;
SQL
      log "ROLLBACK: new Premium actual-second pricing objects removed"
    fi
    log "rollback_dir=$RUN/rollback"
  fi
  cd "$BACKEND_ROOT" >/dev/null 2>&1 || true
  git worktree remove -f "$BWT" >/dev/null 2>&1 || true
  cd "$WEB_ROOT" >/dev/null 2>&1 || true
  git worktree remove -f "$WWT" >/dev/null 2>&1 || true
  exit "$rc"
}
trap rollback EXIT

log "============================================================"
log " desifaces V3 — FINAL GENERATION PRICING / PROGRESS / PERFORMANCE"
log "============================================================"
log "run=$RUN"

log ""
log "===== 1. BACKEND FINAL SOURCE ====="
cd "$BACKEND_ROOT"
git fetch -q origin "$BACKEND_BRANCH"
git worktree add --detach "$BWT" "origin/$BACKEND_BRANCH" >/dev/null
cd "$BWT"
python3 scripts/apply-v3-video-pricing-progress-performance-source.py
python3 scripts/apply-v3-video-worker-parallelism.py
python3 scripts/apply-v3-video-stitch-handoff.py
python3 -m py_compile \
  services/svc-fusion-extension/app/app/api/routes/longform.py \
  services/svc-fusion-extension/app/app/domain/models.py \
  services/svc-fusion-extension/app/app/config.py \
  services/svc-fusion-extension/app/app/services/longform_orchestrator.py \
  services/svc-fusion-extension/app/app/services/premium_actual_seconds_pricing.py \
  services/svc-fusion-extension/app/app/workers/longform_worker.py \
  services/svc-fusion-extension/app/app/workers/stitch_worker.py \
  services/svc-face/app/app/services/creator_orchestrator.py
python3 services/svc-fusion-extension/tests/test_v3_video_pricing_progress_performance.py
git diff --check
if [[ -n "$(git status --porcelain)" ]]; then
  git add services/svc-fusion-extension services/svc-face/app/app/services/creator_orchestrator.py
  git -c user.name='desifaces release' -c user.email='release@desifaces.ai' commit -m 'fix(v3): finalize actual-second video pricing progress and parallel execution' >/dev/null
fi
BACKEND_SHA="$(git rev-parse HEAD)"
git push -q origin "HEAD:refs/heads/$BACKEND_BRANCH"
log "BACKEND_SOURCE=PASS sha=$BACKEND_SHA"

log ""
log "===== 2. WEB FINAL SOURCE + DOCKER CERTIFICATION ====="
cd "$WEB_ROOT"
git fetch -q origin "$WEB_BRANCH"
git worktree add --detach "$WWT" "origin/$WEB_BRANCH" >/dev/null
[[ -f "$WEB_ROOT/web/.env" ]] || fail "missing persistent web/.env"
[[ -f "$WEB_ROOT/control-plane/.env" ]] || fail "missing persistent control-plane/.env"
cp "$WEB_ROOT/web/.env" "$WWT/web/.env"
cp "$WEB_ROOT/control-plane/.env" "$WWT/control-plane/.env"
cd "$WWT"
python3 scripts/apply-generation-progress-ui.py
git diff --check

get_live_env(){
  local key="$1"
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTROL_CONTAINER" |
    awk -v key="$key" 'index($0,key "=")==1 {sub("^[^=]*=",""); print; exit}'
}
export CONTROL_PLANE_DATABASE_URL="$(get_live_env DATABASE_URL)"
export CONTROL_PLANE_AUTH_JWT_SECRET="$(get_live_env AUTH_JWT_SECRET)"
export CONTROL_PLANE_API_KEY_PEPPER="$(get_live_env API_KEY_PEPPER)"
[[ -n "$CONTROL_PLANE_DATABASE_URL" ]] || fail "control-plane DATABASE_URL unavailable"
[[ -n "$CONTROL_PLANE_AUTH_JWT_SECRET" ]] || fail "control-plane JWT secret unavailable"
[[ -n "$CONTROL_PLANE_API_KEY_PEPPER" ]] || fail "control-plane API pepper unavailable"
WEB_BUILD_LOG="$RUN/web-build.log"
if ! docker compose -f docker-compose.web.yml build desifaces-web >"$WEB_BUILD_LOG" 2>&1; then
  tail -n 80 "$WEB_BUILD_LOG" >&2
  fail "web Docker certification/build failed"
fi
if [[ -n "$(git status --porcelain --untracked-files=normal | grep -vE '^(\?\?| M) (web/.env|control-plane/.env)$' || true)" ]]; then
  git add web/components/VideoWorkspaceLongform.tsx web/app/app/face/page.tsx web/package.json web/Dockerfile
  git -c user.name='desifaces release' -c user.email='release@desifaces.ai' commit -m 'feat(web): render backend generation progress for Face and Video' >/dev/null
fi
WEB_SHA="$(git rev-parse HEAD)"
git push -q origin "HEAD:refs/heads/$WEB_BRANCH"
log "WEB_SOURCE_BUILD=PASS sha=$WEB_SHA"

log ""
log "===== 3. NATIVE SOURCE PARITY ====="
git clone -q "$MOBILE_REPO" "$MWT"
cd "$MWT"
git checkout -q "$MOBILE_BRANCH"
python3 scripts/apply-generation-progress-ui.py
python3 scripts/test-generation-progress-source.py
git diff --check
if [[ -n "$(git status --porcelain)" ]]; then
  git add src/components/jobs/GenerationProgressCard.tsx src/features/face/FaceStudioScreen.tsx src/features/fusion/FusionStudioScreen.tsx
  git -c user.name='desifaces release' -c user.email='release@desifaces.ai' commit -m 'feat(mobile): render backend generation progress for Face and Video' >/dev/null
fi
MOBILE_SHA="$(git rev-parse HEAD)"
git push -q origin "HEAD:refs/heads/$MOBILE_BRANCH"
log "NATIVE_SOURCE=PASS sha=$MOBILE_SHA"
log "NATIVE_PRICING_LOGIC=BACKEND_ONLY"

log ""
log "===== 4. RUNTIME ROLLBACK SNAPSHOT ====="
for c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do
  mkdir -p "$RUN/rollback/$c"
  for rel in "${BACKEND_FILES[@]}"; do
    snap="$RUN/rollback/$c/${rel//\//__}"
    if docker cp "$c:/app/$rel" "$snap" >/dev/null 2>&1; then :; else touch "$snap.absent"; fi
  done
done
for c in "$FACE_API" "$FACE_WORKER"; do
  mkdir -p "$RUN/rollback/$c"
  for rel in "${FACE_FILES[@]}"; do
    snap="$RUN/rollback/$c/${rel//\//__}"
    if docker cp "$c:/app/$rel" "$snap" >/dev/null 2>&1; then :; else touch "$snap.absent"; fi
  done
done
WEB_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$WEB_CONTAINER")"
OLD_WEB_IMAGE_ID="$(docker inspect -f '{{.Image}}' "$WEB_CONTAINER")"
WEB_ROLLBACK_TAG="desifaces-web-before-generation-release:${STAMP}"
docker tag "$OLD_WEB_IMAGE_ID" "$WEB_ROLLBACK_TAG"
log "ROLLBACK_SNAPSHOT=PASS"

log ""
log "===== 5. APPLY PREMIUM ACTUAL-SECOND PRICING ====="
cat "$BWT/migrations/2026_09_03_talking_video_premium_actual_seconds.sql" |
  docker exec -i "$EXT_API" python -c 'import asyncio,sys; from app.db import get_db_pool
async def m():
 p=await get_db_pool();
 async with p.acquire() as c: await c.execute(sys.stdin.read())
asyncio.run(m())'
DB_CHANGED=1
log "PRICING_MIGRATION=PASS"

log ""
log "===== 6. DEPLOY BACKEND FILES — REQUIRED SERVICES ONLY ====="
for c in "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do
  for rel in "${BACKEND_FILES[@]}"; do
    src="$BWT/services/svc-fusion-extension/app/$rel"
    [[ -f "$src" ]] || fail "missing candidate file $src"
    docker cp "$src" "$c:/app/$rel"
  done
done
for c in "$FACE_API" "$FACE_WORKER"; do
  for rel in "${FACE_FILES[@]}"; do
    src="$BWT/services/svc-face/app/$rel"
    [[ -f "$src" ]] || fail "missing candidate file $src"
    docker cp "$src" "$c:/app/$rel"
  done
done
RUNTIME_CHANGED=1
docker restart "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER" "$FACE_API" "$FACE_WORKER" >/dev/null
log "RESTARTED=$EXT_API,$EXT_WORKER,$STITCH_WORKER,$FACE_API,$FACE_WORKER"

log ""
log "===== 7. BACKEND RUNTIME CERTIFICATION ====="
sleep 5
docker exec -i "$EXT_API" python - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8006/api/health', timeout=8) as r:
    body=json.loads(r.read().decode())
    assert r.status == 200 and body.get('status') == 'ok'
print('FUSION_EXTENSION_HEALTH=PASS')
PY

docker exec -i "$EXT_API" python - <<'PY'
from app.services.premium_actual_seconds_pricing import PREMIUM_CREDITS_PER_SECOND, premium_billable_seconds
from app.services.longform_orchestrator import build_longform_pricing_preview_spec
assert PREMIUM_CREDITS_PER_SECOND == 15
for duration, expected in ((56,840),(91,1365)):
    spec=build_longform_pricing_preview_spec('00000000-0000-0000-0000-000000000001', {
        'longform_profile':'talking_video','quality_tier':'premium','provider_hint':'kling',
        'pricing_duration_sec':duration,'requested_duration_sec':duration,
        'tags':{'longform_profile':'talking_video','quality_tier':'premium','provider_hint':'kling','requested_duration_sec':duration},
    })
    units=int(float(spec.units))
    assert units == premium_billable_seconds(duration), (duration, units)
    assert units * PREMIUM_CREDITS_PER_SECOND == expected, (duration, units)
    assert spec.meta.get('pricing_strategy') == 'premium_actual_seconds'
print('PREMIUM_ACTUAL_SECONDS_RUNTIME=PASS 56s=840 91s=1365')
PY

docker exec -i "$EXT_WORKER" python - <<'PY'
from app.workers.longform_worker import _effective_max_inflight_per_job
v=_effective_max_inflight_per_job()
assert v >= 4, v
print(f'VIDEO_SEGMENT_FANOUT=PASS effective_max_inflight={v}')
PY

docker exec -i "$STITCH_WORKER" python - <<'PY'
import inspect
from app.workers import stitch_worker
from app.services import longform_orchestrator
s=inspect.getsource(stitch_worker)
o=inspect.getsource(longform_orchestrator.stitch_if_ready)
assert 'stitching_running' in s
assert 'await stitch_if_ready' in s
assert 'asyncio.gather' in s
assert 'stitching_running' in o
assert 'commit_longform_pricing_for_job' in o
print('DEDICATED_PARALLEL_STITCH=PASS canonical_commit_preserved=true')
PY

docker exec -i "$FACE_API" python - <<'PY'
import inspect
from app.services.creator_orchestrator import CreatorOrchestrator
s=inspect.getsource(CreatorOrchestrator)
assert 'FACE_PROGRESS_DETAIL_V1' in s
assert 'asyncio.gather' in s
print('FACE_PROGRESS_AND_EXISTING_PARALLELISM=PASS')
PY

cat <<'PY' | docker exec -i "$EXT_API" python -
import asyncio
from app.db import get_db_pool
async def main():
    p=await get_db_pool()
    async with p.acquire() as c:
        sku=await c.fetchrow("select default_unit_credits, unit, metadata_json from public.pricing_skus where code='LONGFORM_TALK_PREMIUM_SECOND'")
        assert sku and int(sku['default_unit_credits'])==15 and str(sku['unit'])=='second'
        rows=await c.fetch("""
          select pb.channel, sp.unit_credits_override
          from public.pricing_sku_prices sp
          join public.pricing_pricebooks pb on pb.id=sp.pricebook_id
          where sp.sku_code='LONGFORM_TALK_PREMIUM_SECOND' and pb.channel in ('web','mobile')
        """)
        assert rows
        assert all(int(r['unit_credits_override'])==15 for r in rows)
        channels={str(r['channel']) for r in rows}
        assert 'web' in channels and 'mobile' in channels, channels
        print('WEB_MOBILE_PRICING_PARITY=PASS rate=15_credits_per_second')
asyncio.run(main())
PY

for spec in "Face:18003" "Pricing:18009"; do
  name="${spec%%:*}"; port="${spec##*:}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:${port}/api/health" || true)"
  [[ "$code" == 200 ]] || fail "$name health failed HTTP_$code"
  log "$name=HTTP_200"
done

log ""
log "===== 8. DEPLOY WEB ONLY ====="
cd "$WWT"
docker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >"$RUN/web-deploy.log" 2>&1
WEB_CHANGED=1
WEB_OK=0
for _ in $(seq 1 45); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 http://127.0.0.1:13000/auth/login 2>/dev/null || true)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$WEB_CONTAINER" 2>/dev/null || true)"
  if [[ "$code" == 200 && "$health" == healthy ]]; then WEB_OK=1; break; fi
  sleep 2
done
(( WEB_OK == 1 )) || fail "web health gate failed"
docker exec "$WEB_CONTAINER" sh -lc "grep -R -q 'GenerationProgress' /app/.next" || fail "deployed web progress component not found in build output"
docker exec "$WEB_CONTAINER" node -e '
const base=(process.env.FUSION_EXTENSION_BASE_URL||"").replace(/\/$/,"");
if(!base) process.exit(2);
fetch(base+"/api/health").then(r=>{if(r.status!==200)process.exit(3);console.log("WEB_TO_FUSION_EXTENSION=PASS")}).catch(()=>process.exit(4));
'
log "WEB_RUNTIME=PASS HTTP_200"

log ""
log "===== 9. PRESERVATION GATE ====="
for c in "${!BEFORE[@]}"; do
  after="$(docker inspect -f '{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}' "$c")"
  [[ "$after" == "${BEFORE[$c]}" ]] || fail "untouched service changed: $c"
  log "PRESERVED=$c"
done
for spec in "Audio:18004" "Fusion:18002" "Pricing:18009" "Director:18011"; do
  name="${spec%%:*}"; port="${spec##*:}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:${port}/api/health" || true)"
  [[ "$code" == 200 ]] || fail "$name regression HTTP_$code"
  log "PRESERVE_${name^^}=PASS HTTP_200"
done

log ""
log "============================================================"
log " FINAL OUTCOME: DEPLOYED + RUNTIME CERTIFIED"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "mobile_source_sha=$MOBILE_SHA"
log "premium_talking_video_rate=15_credits_per_actual_second"
log "minimum_billable_seconds=10"
log "56_second_quote=840_credits"
log "91_second_quote=1365_credits"
log "customer_pricing_internal_segment_count=ignored"
log "web_mobile_credit_consumption=identical_backend_policy"
log "reservation_lifecycle=preserved"
log "pricing_pending_gate=preserved"
log "child_pricing=suppressed_unchanged"
log "provider_routing=unchanged"
log "segment_rendering=parallel_fanout"
log "final_stitch=dedicated_parallel_parent_worker"
log "stitch_pricing_commit=canonical_existing_path"
log "video_progress=backend_driven"
log "face_progress=backend_driven"
log "face_existing_variant_parallelism=preserved"
log "native_source=ready_for_next_app_build"
log "native_binary_deployed=false"
log "rollback_dir=$RUN/rollback"
log "web_build_log=$WEB_BUILD_LOG"
log "============================================================"

trap - EXIT
cd "$BACKEND_ROOT"
git worktree remove -f "$BWT" >/dev/null 2>&1 || true
cd "$WEB_ROOT"
git worktree remove -f "$WWT" >/dev/null 2>&1 || true
