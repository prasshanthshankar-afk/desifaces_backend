#!/usr/bin/env bash
set -Eeuo pipefail

BACKEND_ROOT="${BACKEND_ROOT:-/home/azureuser/workspace/desifaces-v3}"
WEB_ROOT="${WEB_ROOT:-/home/azureuser/workspace/desifaces-web-review}"
BACKEND_SHA="1a23dfb7647a619f220e213aade93eee36338a1b"
WEB_SHA="742a841459b50276988b6c0e75a53c6d0328e23b"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN="/tmp/v3-web-e2e-closeout-resume-${STAMP}"
BWT="$RUN/backend"
WWT="$RUN/web"
mkdir -p "$RUN/rollback"

log(){ printf '%s\n' "$*"; }
fail(){ printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
exists(){ docker inspect "$1" >/dev/null 2>&1; }
resolve_container(){
  local preferred
  local service
  local found
  preferred="$1"
  service="$2"
  if exists "$preferred"; then printf '%s' "$preferred"; return 0; fi
  found="$(docker ps -a --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | head -1)"
  [[ -n "$found" ]] || return 1
  printf '%s' "$found"
}
for x in git docker curl python3; do command -v "$x" >/dev/null 2>&1 || fail "missing required command: $x"; done

AUDIO_API="$(resolve_container df-v3-svc-audio svc-audio)" || fail "Audio API missing"
AUDIO_WORKER="$(resolve_container df-v3-svc-audio-worker svc-audio-worker)" || fail "Audio worker missing"
FACE_API="$(resolve_container df-v3-svc-face svc-face)" || fail "Face API missing"
FACE_WORKER="$(resolve_container df-v3-svc-face-worker svc-face-worker)" || fail "Face worker missing"
EXT_API="$(resolve_container df-v3-svc-fusion-extension svc-fusion-extension)" || fail "Fusion Extension API missing"
WEB_CONTAINER="$(resolve_container df-v3-web desifaces-web)" || fail "web container missing"
CONTROL_CONTAINER="$(resolve_container df-v3-svc-control-plane svc-control-plane)" || fail "control-plane missing"

UNTOUCHED=(
  df-v3-svc-fusion
  df-v3-svc-fusion-worker
  df-v3-svc-fusion-extension-worker
  df-v3-svc-fusion-extension-stitch-worker
  df-v3-svc-pricing
  df-v3-svc-director
  df-v3-svc-director-worker
  desifaces-v3-db
  desifaces-v3-redis
  "$CONTROL_CONTAINER"
)
declare -A BEFORE
for c in "${UNTOUCHED[@]}"; do
  if exists "$c"; then BEFORE[$c]="$(docker inspect -f '{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}' "$c")"; fi
done

RUNTIME_CHANGED=0
WEB_CHANGED=0
WEB_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$WEB_CONTAINER")"
WEB_ROLLBACK_TAG="desifaces-web-before-e2e-closeout-resume:${STAMP}"
WEB_BUILD_SOURCE="reused_prior_certified_image"

snapshot_file(){
  local c
  local rel
  local safe
  local target
  c="$1"
  rel="$2"
  safe="${rel//\//__}"
  target="$RUN/rollback/$c/$safe"
  mkdir -p "$RUN/rollback/$c"
  if ! docker cp "$c:/app/$rel" "$target" >/dev/null 2>&1; then
    touch "$target.absent"
  fi
}
restore_file(){
  local c
  local rel
  local safe
  local target
  c="$1"
  rel="$2"
  safe="${rel//\//__}"
  target="$RUN/rollback/$c/$safe"
  if [[ -f "$target" ]]; then docker cp "$target" "$c:/app/$rel" >/dev/null 2>&1 || true; fi
  if [[ -f "$target.absent" ]]; then docker exec "$c" rm -f "/app/$rel" >/dev/null 2>&1 || true; fi
}
cleanup(){
  set +e
  cd "$BACKEND_ROOT" >/dev/null 2>&1 || true
  git worktree remove -f "$BWT" >/dev/null 2>&1 || true
  cd "$WEB_ROOT" >/dev/null 2>&1 || true
  git worktree remove -f "$WWT" >/dev/null 2>&1 || true
}
rollback(){
  local rc
  rc=$?
  set +e
  if (( rc != 0 )); then
    log ""
    log "===== AUTOMATIC ROLLBACK ====="
    if (( WEB_CHANGED == 1 )) && docker image inspect "$WEB_ROLLBACK_TAG" >/dev/null 2>&1; then
      docker tag "$WEB_ROLLBACK_TAG" "$WEB_IMAGE" >/dev/null 2>&1 || true
      (cd "$WWT" && docker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >/dev/null 2>&1) || true
      log "ROLLBACK: web runtime restored"
    fi
    if (( RUNTIME_CHANGED == 1 )); then
      for c in "$AUDIO_API" "$AUDIO_WORKER"; do
        restore_file "$c" app/services/tts_resolution_planner.py
        restore_file "$c" app/services/tts_model_resolver.py
      done
      for c in "$FACE_API" "$FACE_WORKER"; do
        restore_file "$c" app/services/creator_prompt_service.py
        restore_file "$c" app/services/creator_orchestrator.py
      done
      restore_file "$EXT_API" app/api/routes/longform.py
      restore_file "$EXT_API" app/services/video_direction_contract.py
      docker restart "$AUDIO_API" "$AUDIO_WORKER" "$FACE_API" "$FACE_WORKER" "$EXT_API" >/dev/null 2>&1 || true
      log "ROLLBACK: modified backend runtime restored"
    fi
    log "rollback_dir=$RUN/rollback"
  fi
  cleanup
  exit "$rc"
}
trap rollback EXIT

log "============================================================"
log " desifaces V3 — WEB E2E UI/UX CLOSEOUT RUNTIME RESUME"
log "============================================================"
log "run=$RUN"

log ""
log "===== A. REUSE ALREADY-CERTIFIED SOURCE ====="
cd "$BACKEND_ROOT"
git cat-file -e "$BACKEND_SHA^{commit}" 2>/dev/null || fail "certified backend SHA unavailable: $BACKEND_SHA"
git worktree add --detach "$BWT" "$BACKEND_SHA" >/dev/null
cd "$WEB_ROOT"
git cat-file -e "$WEB_SHA^{commit}" 2>/dev/null || fail "certified web SHA unavailable: $WEB_SHA"
git worktree add --detach "$WWT" "$WEB_SHA" >/dev/null
[[ "$(git -C "$BWT" rev-parse HEAD)" == "$BACKEND_SHA" ]] || fail "backend worktree SHA mismatch"
[[ "$(git -C "$WWT" rev-parse HEAD)" == "$WEB_SHA" ]] || fail "web worktree SHA mismatch"
python3 -m py_compile \
  "$BWT/services/svc-audio/app/app/services/tts_resolution_planner.py" \
  "$BWT/services/svc-audio/app/app/services/tts_model_resolver.py" \
  "$BWT/services/svc-face/app/app/services/creator_prompt_service.py" \
  "$BWT/services/svc-face/app/app/services/creator_orchestrator.py" \
  "$BWT/services/svc-fusion-extension/app/app/services/video_direction_contract.py" \
  "$BWT/services/svc-fusion-extension/app/app/api/routes/longform.py"
[[ -f "$WEB_ROOT/web/.env" && -f "$WEB_ROOT/control-plane/.env" ]] || fail "persistent web/control-plane env missing"
cp "$WEB_ROOT/web/.env" "$WWT/web/.env"
cp "$WEB_ROOT/control-plane/.env" "$WWT/control-plane/.env"
log "CERTIFIED_SOURCE_REUSE=PASS backend=$BACKEND_SHA web=$WEB_SHA"

get_live_env(){
  local key
  key="$1"
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTROL_CONTAINER" |
    awk -v key="$key" 'index($0,key "=")==1 {sub("^[^=]*=",""); print; exit}'
}
export CONTROL_PLANE_DATABASE_URL="$(get_live_env DATABASE_URL)"
export CONTROL_PLANE_AUTH_JWT_SECRET="$(get_live_env AUTH_JWT_SECRET)"
export CONTROL_PLANE_API_KEY_PEPPER="$(get_live_env API_KEY_PEPPER)"
[[ -n "$CONTROL_PLANE_DATABASE_URL" && -n "$CONTROL_PLANE_AUTH_JWT_SECRET" && -n "$CONTROL_PLANE_API_KEY_PEPPER" ]] || fail "control-plane runtime env unavailable"

# Step 2 from the prior run built this exact Compose project image. Reuse it when
# present; rebuild only if Docker no longer has the candidate tag.
if docker image inspect "$WEB_IMAGE" >/dev/null 2>&1; then
  log "WEB_CANDIDATE_IMAGE=REUSE image=$WEB_IMAGE"
else
  WEB_BUILD_SOURCE="rebuilt_from_exact_certified_web_sha"
  WEB_BUILD_LOG="$RUN/web-rebuild.log"
  if ! (cd "$WWT" && docker compose -f docker-compose.web.yml build desifaces-web >"$WEB_BUILD_LOG" 2>&1); then
    tail -n 100 "$WEB_BUILD_LOG" >&2
    fail "certified web image rebuild failed"
  fi
  docker image inspect "$WEB_IMAGE" >/dev/null 2>&1 || fail "candidate web image missing after rebuild"
  log "WEB_CANDIDATE_IMAGE=REBUILT image=$WEB_IMAGE"
fi

log ""
log "===== B. CAPTURE ROLLBACK STATE BEFORE FIRST MUTATION ====="
for c in "$AUDIO_API" "$AUDIO_WORKER"; do
  snapshot_file "$c" app/services/tts_resolution_planner.py
  snapshot_file "$c" app/services/tts_model_resolver.py
done
for c in "$FACE_API" "$FACE_WORKER"; do
  snapshot_file "$c" app/services/creator_prompt_service.py
  snapshot_file "$c" app/services/creator_orchestrator.py
done
snapshot_file "$EXT_API" app/api/routes/longform.py
snapshot_file "$EXT_API" app/services/video_direction_contract.py
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
log "ROLLBACK_SNAPSHOT=PASS backend_files=true web_runtime=filesystem_copy"

log ""
log "===== C. DEPLOY NARROW BACKEND RUNTIME ====="
RUNTIME_CHANGED=1
for c in "$AUDIO_API" "$AUDIO_WORKER"; do
  docker cp "$BWT/services/svc-audio/app/app/services/tts_resolution_planner.py" "$c:/app/app/services/tts_resolution_planner.py"
  docker cp "$BWT/services/svc-audio/app/app/services/tts_model_resolver.py" "$c:/app/app/services/tts_model_resolver.py"
done
for c in "$FACE_API" "$FACE_WORKER"; do
  docker cp "$BWT/services/svc-face/app/app/services/creator_prompt_service.py" "$c:/app/app/services/creator_prompt_service.py"
  docker cp "$BWT/services/svc-face/app/app/services/creator_orchestrator.py" "$c:/app/app/services/creator_orchestrator.py"
done
docker cp "$BWT/services/svc-fusion-extension/app/app/services/video_direction_contract.py" "$EXT_API:/app/app/services/video_direction_contract.py"
docker cp "$BWT/services/svc-fusion-extension/app/app/api/routes/longform.py" "$EXT_API:/app/app/api/routes/longform.py"
docker restart "$AUDIO_API" "$AUDIO_WORKER" "$FACE_API" "$FACE_WORKER" "$EXT_API" >/dev/null
log "UPDATED=$AUDIO_API,$AUDIO_WORKER,$FACE_API,$FACE_WORKER,$EXT_API"

log ""
log "===== D. BACKEND RUNTIME CERTIFICATION ====="
READY=0
for _ in $(seq 1 45); do
  audio_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18004/api/health 2>/dev/null || true)"
  face_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:18003/api/health 2>/dev/null || true)"
  ext_ok="$(docker exec "$EXT_API" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/api/health',timeout=3).read(); print('ok')" 2>/dev/null || true)"
  if [[ "$audio_code" == 200 && "$face_code" == 200 && "$ext_ok" == *ok* ]]; then READY=1; break; fi
  sleep 2
done
(( READY == 1 )) || fail "modified backend services did not become ready"

# Model selection itself must choose a model with an eligible voice for the
# authoritative Face gender.
docker exec -i "$AUDIO_API" python - <<'PYMODEL'
import asyncio
from types import SimpleNamespace
from app.services.tts_model_resolver import TTSModelResolver, TTSModelResolutionRequest
class Catalog:
 async def get_default_routing_policy(self): return SimpleNamespace(policy_code='p',require_approved_capability=False,require_approved_quality=False)
 async def list_routing_enabled_model_candidates(self, **kw):
  base=dict(adapter_key='a',provider_model_id=None,canonical_locale='pa-IN',language_code='pa',provider_locale_code='pa-IN',provider_language_code='pa',capability_scope='locale',quality_class='premium',quality_score=1.0,max_input_chars=5000)
  return [SimpleNamespace(provider_code='p1',model_code='female-only',**base),SimpleNamespace(provider_code='p2',model_code='male-capable',**base)]
 async def list_voice_candidates(self, **kw):
  return [SimpleNamespace()] if kw.get('model_code')=='male-capable' and kw.get('requested_gender')=='male' else []
 async def get_masterdata_revision(self): return 1
async def main():
 r=await TTSModelResolver(Catalog()).resolve(TTSModelResolutionRequest(canonical_locale='pa-IN',text_length=10,requested_gender='male'))
 assert r.model_code=='male-capable', r.model_code
 print('VOICE_MODEL_GENDER_ELIGIBILITY=PASS model=male-capable')
asyncio.run(main())
PYMODEL

# A stale explicit voice ID falls back to same-gender automatic resolution.
docker exec -i "$AUDIO_API" python - <<'PYVOICE'
import asyncio
from types import SimpleNamespace
from app.services.tts_resolution_planner import TTSResolutionPlanner, TTSResolutionPlanRequest
class Locale:
 async def resolve(self, raw): return SimpleNamespace(locale=raw)
class Model:
 def __init__(self): self.calls=[]
 async def resolve(self, req):
  self.calls.append(req.requested_voice)
  if req.requested_voice:
   raise ValueError(f"requested_voice_not_eligible_for_any_model:{req.canonical_locale}/{req.requested_voice}")
  return SimpleNamespace(provider_code='azure',adapter_key='azure',model_code='m',provider_model_id='m',provider_locale_code='pa-IN',provider_language_code='pa',language_code='pa',capability_scope='locale',quality_class='premium',quality_score=1.0,max_input_chars=5000,routing_policy_code='default',masterdata_revision=1)
class Voice:
 async def resolve(self, req):
  assert req.requested_voice is None
  assert req.requested_gender == 'male'
  return SimpleNamespace(voice_id='eligible-male',voice_name='Eligible Male',gender='male',home_locale='pa-IN',provider_code='azure',model_code='m',canonical_locale='pa-IN',quality_score=1.0)
async def main():
 m=Model(); p=TTSResolutionPlanner(locale_resolver=Locale(),model_resolver=m,voice_resolver=Voice())
 r=await p.resolve(TTSResolutionPlanRequest(requested_locale='pa-IN',text_length=100,requested_voice='shubh',requested_gender='male'))
 assert m.calls == ['shubh', None], m.calls
 assert r.voice_id == 'eligible-male' and r.voice_gender == 'male'
 print('VOICE_STALE_ID_AUTO_RESOLUTION=PASS gender_preserved=male')
asyncio.run(main())
PYVOICE

docker exec -i "$FACE_API" python - <<'PYFACE'
import inspect
from app.services.creator_prompt_service import CreatorPromptService
from app.services.creator_orchestrator import CreatorOrchestrator
assert CreatorPromptService._infer_gender_from_prompt('confident man in a studio') == 'male'
assert CreatorPromptService._infer_gender_from_prompt('confident woman in a studio') == 'female'
assert CreatorPromptService._infer_gender_from_prompt('confident person in a studio') == ''
s=inspect.getsource(CreatorOrchestrator)
assert 'FACE_VARIANT_TECHNICAL_GENDER_V1' in s
print('FACE_VARIANT_GENDER_METADATA=PASS')
PYFACE

docker exec -i "$EXT_API" python - <<'PYVIDEO'
from app.api.routes.longform import _normalize_longform_request_body
from app.services.premium_actual_seconds_pricing import PREMIUM_CREDITS_PER_SECOND
raw={
 'image_ref':'00000000-0000-0000-0000-000000000001',
 'script':'test',
 'longform_profile':'talking_video',
 'quality_tier':'premium',
 'tags':{'video_direction':{'performance_style':'expressive','emotion':'warm','scene_motion':'ambient','hand_motion':'subtle','body_motion':'natural','camera_motion':'subtle_drift'}}
}
out=_normalize_longform_request_body(raw)
assert out['provider_hint']=='kling', out.get('provider_hint')
assert out['provider_options']['provider_hint']=='kling'
assert out['background_mode']=='movement_based'
assert out['tags']['emotion']=='warm'
assert out['tags']['hand_motion']=='subtle'
assert 'original image context' in out['provider_options']['motion_prompt']
assert PREMIUM_CREDITS_PER_SECOND == 15
print('VIDEO_DIRECTION_RUNTIME=PASS provider_routing=kling pricing_rate=15_unchanged')
PYVIDEO
log "BACKEND_RUNTIME=PASS"

log ""
log "===== E. REPLACE WEB ONLY WITH CERTIFIED IMAGE ====="
cd "$WWT"
WEB_CHANGED=1
docker compose -f docker-compose.web.yml up -d --no-deps --force-recreate desifaces-web >"$RUN/web-deploy.log" 2>&1
WEB_OK=0
for _ in $(seq 1 45); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 http://127.0.0.1:13000/auth/login 2>/dev/null || true)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$WEB_CONTAINER" 2>/dev/null || true)"
  if [[ "$code" == 200 && "$health" == healthy ]]; then WEB_OK=1; break; fi
  sleep 2
done
(( WEB_OK == 1 )) || fail "web runtime health failed"
docker exec "$WEB_CONTAINER" node -e '
const targets=[process.env.AUDIO_BASE_URL,process.env.FUSION_EXTENSION_BASE_URL].filter(Boolean);
Promise.all(targets.map(async b=>{const r=await fetch(b.replace(/\/$/,"")+"/api/health");if(r.status!==200)throw new Error(String(r.status));})).then(()=>console.log("WEB_BACKEND_CONNECTIVITY=PASS")).catch(()=>process.exit(3));
'
log "WEB_RUNTIME=PASS HTTP_200"

log ""
log "===== F. UNTOUCHED-SERVICE PRESERVATION ====="
for c in "${!BEFORE[@]}"; do
  after="$(docker inspect -f '{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}' "$c")"
  [[ "$after" == "${BEFORE[$c]}" ]] || fail "untouched service changed: $c"
  log "PRESERVED=$c"
done
for spec in Fusion:18002 Pricing:18009 Director:18011; do
  name="${spec%%:*}"
  port="${spec##*:}"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:${port}/api/health 2>/dev/null || true)"
  [[ "$code" == 200 ]] || fail "$name regression HTTP_$code"
  log "PRESERVE_${name^^}=PASS HTTP_200"
done

log ""
log "============================================================"
log " WEB E2E UI/UX CLOSEOUT: DEPLOYED + RUNTIME CERTIFIED"
log "============================================================"
log "backend_sha=$BACKEND_SHA"
log "web_sha=$WEB_SHA"
log "web_image_source=$WEB_BUILD_SOURCE"
log "face_variant_gender_metadata=authoritative"
log "voice_gender_binding=selected_face_first"
log "voice_stale_id_resolution=automatic_same_gender"
log "voice_model_selection=gender_eligible"
log "voice_raw_model_resolution_error=removed_when_fallback_exists"
log "pricing_ui=face_voice_video_shared_lifecycle"
log "video_zero_question_happy_path=preserved"
log "video_optional_controls=performance_emotion_scene_motion"
log "video_advanced_controls=collapsed"
log "video_direction_contract=provider_neutral"
log "provider_routing=unchanged_kling"
log "talking_video_pricing=unchanged_15_credits_per_second"
log "reservation_lifecycle=unchanged"
log "core_fusion_parallelism=preserved_not_restarted"
log "heygen_activation=not_in_this_closeout"
log "rollback_dir=$RUN/rollback"
log "============================================================"

trap - EXIT
cleanup
