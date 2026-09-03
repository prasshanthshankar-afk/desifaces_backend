#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
BACKEND_SHA="${BACKEND_SHA:-21267cf8f0a25622ceb83f74e3abb8aea5f11b6a}"
WEB_SHA="${WEB_SHA:-5032eba2e25d0653e1f41d973861024441ae76f8}"
WEB_REPO="${WEB_REPO:-git@github.com:prasshanthshankar-afk/desifaces_web.git}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/desifaces-v3-launch-${STAMP}"
BWT="$RUN/backend"
WWT="$RUN/web"
ROLLBACK="$RUN/rollback"
mkdir -p "$ROLLBACK"

log(){ printf '%s\n' "$*"; }
fail(){ printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }
exists(){ docker inspect "$1" >/dev/null 2>&1; }
for x in git docker curl python3; do need "$x"; done

DB_CONTAINER="${DB_CONTAINER:-desifaces-v3-db}"
PRICING_API="${PRICING_API:-df-v3-svc-pricing}"
AUDIO_API="${AUDIO_API:-df-v3-svc-audio}"
AUDIO_WORKER="${AUDIO_WORKER:-df-v3-svc-audio-worker}"
FACE_API="${FACE_API:-df-v3-svc-face}"
FACE_WORKER="${FACE_WORKER:-df-v3-svc-face-worker}"
FUSION_API="${FUSION_API:-df-v3-svc-fusion}"
FUSION_WORKER="${FUSION_WORKER:-df-v3-svc-fusion-worker}"
EXT_API="${EXT_API:-df-v3-svc-fusion-extension}"
EXT_WORKER="${EXT_WORKER:-df-v3-svc-fusion-extension-worker}"
STITCH_WORKER="${STITCH_WORKER:-df-v3-svc-fusion-extension-stitch-worker}"
WEB_CONTAINER="${WEB_CONTAINER:-df-v3-web}"
CONTROL_CONTAINER="${CONTROL_CONTAINER:-df-v3-svc-control-plane}"

REQUIRED_CONTAINERS=(
  "$DB_CONTAINER" "$PRICING_API"
  "$AUDIO_API" "$AUDIO_WORKER"
  "$FACE_API" "$FACE_WORKER"
  "$FUSION_API" "$FUSION_WORKER"
  "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"
  "$WEB_CONTAINER" "$CONTROL_CONTAINER"
)
for c in "${REQUIRED_CONTAINERS[@]}"; do exists "$c" || fail "required live V3 container not found: $c"; done

# group|source root|container rel paths|containers
SERVICE_GROUPS=(pricing audio face fusion extension)
declare -A SRC_ROOT RELS CONTAINERS
SRC_ROOT[pricing]="services/svc-pricing/app"
RELS[pricing]="app/api/routes/spending.py app/services/customer_spending_service.py app/main.py"
CONTAINERS[pricing]="$PRICING_API"
SRC_ROOT[audio]="services/svc-audio/app"
RELS[audio]="app/services/tts_model_resolver.py app/services/tts_resolution_planner.py"
CONTAINERS[audio]="$AUDIO_API $AUDIO_WORKER"
SRC_ROOT[face]="services/svc-face/app"
RELS[face]="app/services/creator_orchestrator.py app/services/creator_prompt_service.py"
CONTAINERS[face]="$FACE_API $FACE_WORKER"
SRC_ROOT[fusion]="services/svc-fusion/app"
RELS[fusion]="app/workers/fusion_worker.py"
CONTAINERS[fusion]="$FUSION_API $FUSION_WORKER"
SRC_ROOT[extension]="services/svc-fusion-extension/app"
RELS[extension]="app/api/routes/longform.py app/config.py app/domain/models.py app/main.py app/repos/longform_jobs_repo.py app/services/longform_orchestrator.py app/services/longform_pricing_confirmation_policy.py app/services/premium_actual_seconds_pricing.py app/services/video_direction_contract.py app/workers/longform_worker.py app/workers/stitch_worker.py"
CONTAINERS[extension]="$EXT_API $EXT_WORKER $STITCH_WORKER"

RUNTIME_CHANGED=0
DB_CHANGED=0
WEB_CHANGED=0

snapshot_file(){
  local c="$1" rel="$2" dir="$ROLLBACK/$c" snap
  mkdir -p "$dir"
  snap="$dir/${rel//\//__}"
  if docker cp "$c:/app/$rel" "$snap" >/dev/null 2>&1; then :; else touch "$snap.absent"; fi
}
restore_file(){
  local c="$1" rel="$2" snap="$ROLLBACK/$c/${rel//\//__}"
  if [[ -f "$snap" ]]; then
    docker cp "$snap" "$c:/app/$rel" >/dev/null 2>&1 || true
  elif [[ -f "$snap.absent" ]]; then
    docker exec "$c" rm -f "/app/$rel" >/dev/null 2>&1 || true
  fi
}

cleanup(){
  set +e
  if [[ -d "$BWT" ]]; then git -C "$BACKEND_ROOT" worktree remove -f "$BWT" >/dev/null 2>&1 || true; fi
  if [[ -d "$WWT/.git" || -f "$WWT/.git" ]]; then git -C "$WWT" status >/dev/null 2>&1 || true; fi
}
rollback(){
  local rc=$?
  set +e
  if (( rc != 0 )); then
    log ""
    log "===== AUTOMATIC ROLLBACK ====="
    if (( RUNTIME_CHANGED == 1 )); then
      for g in "${SERVICE_GROUPS[@]}"; do
        for c in ${CONTAINERS[$g]}; do
          for rel in ${RELS[$g]}; do restore_file "$c" "$rel"; done
        done
      done
      docker restart "$PRICING_API" "$AUDIO_API" "$AUDIO_WORKER" "$FACE_API" "$FACE_WORKER" "$FUSION_API" "$FUSION_WORKER" "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER" >/dev/null 2>&1 || true
      log "ROLLBACK_RUNTIME=ATTEMPTED"
    fi
    if (( DB_CHANGED == 1 )); then
      db_user="$(docker exec "$DB_CONTAINER" printenv POSTGRES_USER 2>/dev/null || true)"
      db_name="$(docker exec "$DB_CONTAINER" printenv POSTGRES_DB 2>/dev/null || true)"
      if [[ -n "$db_user" && -n "$db_name" ]]; then
        docker exec -i "$DB_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" <<'SQL' >/dev/null 2>&1 || true
BEGIN;
DELETE FROM public.pricing_sku_prices WHERE sku_code='LONGFORM_TALK_PREMIUM_SECOND';
DELETE FROM public.pricing_variant_lines WHERE variant_code='TALKING_VIDEO_PREMIUM_SECOND';
DELETE FROM public.pricing_variants WHERE code='TALKING_VIDEO_PREMIUM_SECOND';
DELETE FROM public.pricing_skus WHERE code='LONGFORM_TALK_PREMIUM_SECOND';
COMMIT;
SQL
        log "ROLLBACK_NEW_PRICING_OBJECTS=ATTEMPTED"
      fi
    fi
    log "rollback_dir=$ROLLBACK"
  fi
  cleanup
  exit "$rc"
}
trap rollback EXIT

wait_container(){
  local c="$1" status="" health=""
  for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null || true)"
    if [[ "$status" == "running" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then return 0; fi
    [[ "$status" == "exited" || "$status" == "dead" ]] && break
    sleep 2
  done
  docker ps -a --filter "name=^/${c}$" || true
  docker logs --tail 120 "$c" 2>/dev/null || true
  fail "$c did not become ready"
}

make_image_durable(){
  local c="$1" g="$2" orig tmp candidate safe
  orig="$(docker inspect -f '{{.Config.Image}}' "$c")"
  [[ -n "$orig" ]] || { log "WARN: no configured image tag for $c"; return 0; }
  if [[ "$orig" == sha256:* ]]; then log "WARN: $c image is digest-only; runtime source is certified but local tag durability skipped"; return 0; fi
  safe="$(printf '%s' "$c" | tr -c 'A-Za-z0-9_.-' '-')"
  candidate="desifaces-launch-${safe}:${STAMP}"
  tmp="df-launch-image-${safe}-${STAMP}"
  docker create --name "$tmp" "$orig" >/dev/null
  for rel in ${RELS[$g]}; do docker cp "$BWT/${SRC_ROOT[$g]}/$rel" "$tmp:/app/$rel"; done
  docker commit "$tmp" "$candidate" >/dev/null
  docker rm "$tmp" >/dev/null
  docker tag "$candidate" "$orig"
  log "DURABLE_IMAGE=PASS container=$c configured_tag=$orig candidate=$candidate"
}

log "============================================================"
log " desifaces V3 — 2026-09-03 PRODUCTION CUTOVER"
log "============================================================"
log "run=$RUN"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"

log ""
log "===== 1. PREPARE PINNED BACKEND SOURCE ====="
[[ -d "$BACKEND_ROOT/.git" ]] || fail "backend repository missing at $BACKEND_ROOT"
git -C "$BACKEND_ROOT" fetch -q origin main
git -C "$BACKEND_ROOT" cat-file -e "$BACKEND_SHA^{commit}" || fail "backend launch commit not available"
git -C "$BACKEND_ROOT" worktree add --detach "$BWT" "$BACKEND_SHA" >/dev/null
cd "$BWT"
[[ "$(git rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend pin mismatch"
python3 scripts/test-v3-spending-reporting.py
python3 -m py_compile \
  services/svc-pricing/app/app/api/routes/spending.py \
  services/svc-pricing/app/app/services/customer_spending_service.py \
  services/svc-pricing/app/app/main.py \
  services/svc-audio/app/app/services/tts_model_resolver.py \
  services/svc-audio/app/app/services/tts_resolution_planner.py \
  services/svc-face/app/app/services/creator_orchestrator.py \
  services/svc-face/app/app/services/creator_prompt_service.py \
  services/svc-fusion/app/app/workers/fusion_worker.py \
  services/svc-fusion-extension/app/app/api/routes/longform.py \
  services/svc-fusion-extension/app/app/config.py \
  services/svc-fusion-extension/app/app/domain/models.py \
  services/svc-fusion-extension/app/app/main.py \
  services/svc-fusion-extension/app/app/repos/longform_jobs_repo.py \
  services/svc-fusion-extension/app/app/services/longform_orchestrator.py \
  services/svc-fusion-extension/app/app/services/longform_pricing_confirmation_policy.py \
  services/svc-fusion-extension/app/app/services/premium_actual_seconds_pricing.py \
  services/svc-fusion-extension/app/app/services/video_direction_contract.py \
  services/svc-fusion-extension/app/app/workers/longform_worker.py \
  services/svc-fusion-extension/app/app/workers/stitch_worker.py
git diff --check
log "BACKEND_SOURCE_GATE=PASS"

log ""
log "===== 2. SNAPSHOT LIVE SERVICE FILES ====="
for g in "${SERVICE_GROUPS[@]}"; do
  for c in ${CONTAINERS[$g]}; do
    for rel in ${RELS[$g]}; do snapshot_file "$c" "$rel"; done
  done
done
log "RUNTIME_SNAPSHOT=PASS dir=$ROLLBACK"

log ""
log "===== 3. APPLY PREMIUM PRICING MIGRATION ONLY IF NEEDED ====="
db_user="$(docker exec "$DB_CONTAINER" printenv POSTGRES_USER)"
db_name="$(docker exec "$DB_CONTAINER" printenv POSTGRES_DB)"
[[ -n "$db_user" && -n "$db_name" ]] || fail "database identity unavailable"
preexisting="$(docker exec "$DB_CONTAINER" psql -Atq -U "$db_user" -d "$db_name" -c "SELECT 1 FROM public.pricing_skus WHERE code='LONGFORM_TALK_PREMIUM_SECOND' LIMIT 1;" 2>/dev/null || true)"
if [[ "$preexisting" == "1" ]]; then
  log "PRICING_MIGRATION=ALREADY_PRESENT"
else
  docker exec -i "$DB_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" < "$BWT/migrations/2026_09_03_talking_video_premium_actual_seconds.sql" >/tmp/desifaces-launch-migration.log
  DB_CHANGED=1
  post="$(docker exec "$DB_CONTAINER" psql -Atq -U "$db_user" -d "$db_name" -c "SELECT 1 FROM public.pricing_skus WHERE code='LONGFORM_TALK_PREMIUM_SECOND' LIMIT 1;")"
  [[ "$post" == "1" ]] || fail "pricing migration did not converge"
  log "PRICING_MIGRATION=PASS newly_applied=YES"
fi

log ""
log "===== 4. DEPLOY PINNED SOURCE TO AFFECTED V3 SERVICES ====="
RUNTIME_CHANGED=1
for g in "${SERVICE_GROUPS[@]}"; do
  for c in ${CONTAINERS[$g]}; do
    for rel in ${RELS[$g]}; do
      src="$BWT/${SRC_ROOT[$g]}/$rel"
      [[ -f "$src" ]] || fail "candidate file missing: $src"
      docker cp "$src" "$c:/app/$rel"
    done
  done
done
docker restart "$PRICING_API" "$AUDIO_API" "$AUDIO_WORKER" "$FACE_API" "$FACE_WORKER" "$FUSION_API" "$FUSION_WORKER" "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER" >/dev/null
for c in "$PRICING_API" "$AUDIO_API" "$AUDIO_WORKER" "$FACE_API" "$FACE_WORKER" "$FUSION_API" "$FUSION_WORKER" "$EXT_API" "$EXT_WORKER" "$STITCH_WORKER"; do wait_container "$c"; done
log "BACKEND_RUNTIME_READY=PASS"

log ""
log "===== 5. CERTIFY SPENDING + VIDEO CONTRACTS IN LIVE CONTAINERS ====="
docker exec -i "$PRICING_API" python - <<'PY'
from app.main import app
routes={(r.path,m) for r in app.routes for m in getattr(r,'methods',set())}
required={('/api/pricing/me/spending/summary','GET'),('/api/pricing/me/spending/transactions','GET')}
missing=required-routes
assert not missing, missing
print('SPENDING_ROUTES=PASS')
PY
docker exec -i "$EXT_API" python - <<'PY'
from app.services.video_direction_contract import normalize_video_direction
v=normalize_video_direction({'performance_style':'natural','emotion':'auto','scene_motion':'auto','hand_motion':'auto','body_motion':'auto','camera_motion':'auto'})
assert v
print('VIDEO_DIRECTION_CONTRACT=PASS')
PY
log "LIVE_CONTRACT_CERTIFICATION=PASS"

log ""
log "===== 6. MAKE CURRENT SERVICE SOURCE DURABLE IN LOCAL IMAGE TAGS ====="
# One representative per service image family is enough when API/workers share the tag;
# if tags differ, patch each distinct container tag.
declare -A SEEN_TAGS
for g in "${SERVICE_GROUPS[@]}"; do
  for c in ${CONTAINERS[$g]}; do
    tag="$(docker inspect -f '{{.Config.Image}}' "$c")"
    key="$g|$tag"
    if [[ -z "${SEEN_TAGS[$key]:-}" ]]; then
      make_image_durable "$c" "$g"
      SEEN_TAGS[$key]=1
    fi
  done
done
log "BACKEND_IMAGE_DURABILITY=PASS"

log ""
log "===== 7. PREPARE PINNED WEB SOURCE ====="
WEB_BASE=""
for p in \
  /home/azureuser/workspace/desifaces-web-review \
  /home/azureuser/workspace/desifaces-web \
  /home/azureuser/workspace/desifaces_web; do
  if [[ -d "$p/.git" ]]; then WEB_BASE="$p"; break; fi
done
[[ -n "$WEB_BASE" ]] || fail "existing web repository not found; expected one of the standard V3 paths"
git -C "$WEB_BASE" fetch -q origin main
git -C "$WEB_BASE" cat-file -e "$WEB_SHA^{commit}" || fail "web launch commit not available"
git -C "$WEB_BASE" worktree add --detach "$WWT" "$WEB_SHA" >/dev/null
[[ -f "$WEB_BASE/web/.env" ]] || fail "persistent web/.env missing in $WEB_BASE"
[[ -f "$WEB_BASE/control-plane/.env" ]] || fail "persistent control-plane/.env missing in $WEB_BASE"
cp "$WEB_BASE/web/.env" "$WWT/web/.env"
cp "$WEB_BASE/control-plane/.env" "$WWT/control-plane/.env"
[[ "$(git -C "$WWT" rev-parse HEAD)" == "$WEB_SHA" ]] || fail "web pin mismatch"
log "WEB_SOURCE_PIN=PASS source=$WEB_BASE"

log ""
log "===== 8. BUILD + DEPLOY WEB AND CONTROL PLANE ====="
WEB_CHANGED=1
(cd "$WWT" && bash scripts/deploy-and-test-web.sh)
log "WEB_DEPLOY=PASS"

log ""
log "===== 9. FINAL CROSS-CHANNEL VM CERTIFICATION ====="
for c in "$PRICING_API" "$AUDIO_API" "$FACE_API" "$FUSION_API" "$EXT_API" "$WEB_CONTAINER" "$CONTROL_CONTAINER"; do wait_container "$c"; done
web_code="$(curl -sS -o /tmp/desifaces-launch-login.html -w '%{http_code}' http://127.0.0.1:13000/auth/login)"
[[ "$web_code" == "200" ]] || fail "web login smoke returned HTTP $web_code"
grep -qi 'desifaces' /tmp/desifaces-launch-login.html || fail "web login smoke missing desifaces branding"
log "FINAL_WEB_HTTP=PASS"

log ""
log "============================================================"
log " LAUNCH CUTOVER PASS"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "spending_api=READY"
log "voice_video_runtime=READY"
log "web_runtime=READY"
log "rollback_dir=$ROLLBACK"
log "run_dir=$RUN"

# Successful launch: do not invoke rollback; remove only temporary worktrees.
RUNTIME_CHANGED=0
DB_CHANGED=0
cleanup
trap - EXIT
exit 0
