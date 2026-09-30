#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }
TARGET_SHA="${TARGET_SHA:?TARGET_SHA is required}"
SHORT="${TARGET_SHA:0:12}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "============================================================"
echo " desifaces DEV — NARROW SARVAM AUDIO ENABLEMENT"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "scope=SARVAM_SUPPORTED_LOCALES_ONLY"
echo "db_mutation=NONE"
echo "face_video_workflows=FROZEN"
echo "production=UNTOUCHED"

REPO=""
for p in "$HOME/workspace/desifaces-v3" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-backend"; do
  if [[ -d "$p/.git" ]]; then REPO="$p"; break; fi
done
[[ -n "$REPO" ]] || { echo "FAIL: backend repo not found"; exit 2; }

WT="/tmp/df-sarvam-narrow-$SHORT"
rm -rf "$WT"
git -C "$REPO" fetch origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$REPO" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null
cleanup(){ git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo
echo "===== 1. SOURCE REGRESSION CONTRACT ====="
python3 - "$WT/services/svc-audio/app/app/services/tts_service.py" "$WT/services/svc-audio/app/app/services/tts_intent_policy.py" <<'PY'
from pathlib import Path
import sys
svc=Path(sys.argv[1]).read_text()
policy=Path(sys.argv[2]).read_text()

assert "relax_native_style_only_for_eligible_sarvam_voice" in svc
assert "_is_explicit_sarvam_voice_eligible" in svc
assert "requires_style = bool(style)" in svc
assert "v.provider = 'sarvam'" in svc
assert "m.model_code = 'bulbul_v3'" in svc

assert "_SARVAM_RELAXED_PRODUCT_INTENTS" in policy
assert '"conversational"' in policy
assert '"narration"' in policy
assert "if not sarvam_voice_eligible" in policy

# The prior broader behavior must be gone.
assert "requires_provider_native_style" not in svc
assert "requires_provider_native_style" not in policy

print("SARVAM_NARROW_SOURCE_CONTRACT=PASS")
PY

echo
echo "===== 2. BUILD AUDIO CANDIDATE ====="
AUDIO_IMG="df-audio-sarvam-narrow:$SHORT"
docker build -f "$WT/services/svc-audio/app/Dockerfile" -t "$AUDIO_IMG" "$WT" >/tmp/df-sarvam-narrow-build.log 2>&1
echo "AUDIO_BUILD=PASS image=$AUDIO_IMG"

echo
echo "===== 3. IN-IMAGE REGRESSION TEST ====="
docker run --rm "$AUDIO_IMG" python -m py_compile   /app/app/services/tts_service.py   /app/app/services/tts_intent_policy.py   /app/app/services/tts_model_resolver.py   /app/app/services/tts_resolution_planner.py   /app/app/services/sarvam_tts_adapter.py

docker run --rm -i "$AUDIO_IMG" python - <<'PY'
from app.services.tts_intent_policy import relax_native_style_only_for_eligible_sarvam_voice as relax

assert relax("Conversational",sarvam_voice_eligible=False) is False
assert relax("Narration",sarvam_voice_eligible=False) is False
assert relax("Conversational",sarvam_voice_eligible=True) is True
assert relax("Narration",sarvam_voice_eligible=True) is True
assert relax("Character",sarvam_voice_eligible=True) is False
assert relax("cheerful",sarvam_voice_eligible=True) is False
assert relax(None,sarvam_voice_eligible=True) is False
print("NON_SARVAM_STYLE_SEMANTICS_UNCHANGED=PASS")
print("SARVAM_NARROW_STYLE_COMPATIBILITY=PASS")
print("CHARACTER_STYLE_GATE_UNCHANGED=PASS")
PY

echo
echo "===== 4. LIVE DB READ-ONLY CAPABILITY GATE ====="
AUDIO_API="${AUDIO_API:-df-svc-audio}"
AUDIO_WORKER="${AUDIO_WORKER:-df-svc-audio-worker}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

for c in "$AUDIO_API" "$AUDIO_WORKER" "$DB_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c"; exit 2; }
done

DB_URL="$(docker exec "$AUDIO_API" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

CAPS="$("${PSQL[@]}" -Atq -c "
select string_agg(locale,',' order by locale)
from public.tts_model_locale_capabilities
where provider_code='sarvam'
  and model_code='bulbul_v3'
  and is_enabled=true
  and is_approved=true;
")"

EXPECTED="bn-IN,en-IN,gu-IN,hi-IN,kn-IN,ml-IN,mr-IN,or-IN,pa-IN,ta-IN,te-IN"
[[ "$CAPS" == "$EXPECTED" ]] || {
  echo "FAIL: Sarvam locale capability set changed"
  echo "actual=$CAPS"
  exit 3
}
echo "SARVAM_SUPPORTED_LOCALE_SET=PASS locales=$CAPS"

GLOBAL_SARVAM="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.tts_model_locale_capabilities
where provider_code='sarvam'
  and model_code='bulbul_v3'
  and is_enabled=true
  and is_approved=true
  and locale in ('en-US','de-DE','fr-FR','es-ES');
")"
[[ "$GLOBAL_SARVAM" == "0" ]] || { echo "FAIL: Sarvam leaked into global locale capability"; exit 3; }
echo "SARVAM_GLOBAL_LOCALE_LEAK=NONE"

echo
echo "===== 5. CANONICAL COMPOSE ENV ====="
PROJECT="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
PROJECT_DIR="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
CONFIG_FILES="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
API_SERVICE="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.service"}}')"
WORKER_SERVICE="$(docker inspect "$AUDIO_WORKER" --format '{{index .Config.Labels "com.docker.compose.service"}}')"

[[ -n "$PROJECT" && -n "$PROJECT_DIR" && -d "$PROJECT_DIR" && -n "$CONFIG_FILES" && -n "$API_SERVICE" && -n "$WORKER_SERVICE" ]] || {
  echo "FAIL: compose ownership metadata incomplete"; exit 4;
}

IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
COMPOSE_FILES=()
for f in "${CFG_ARR[@]}"; do
  [[ "$f" = /* ]] || f="$PROJECT_DIR/$f"
  [[ -f "$f" ]] || { echo "FAIL: compose file missing: $f"; exit 4; }
  COMPOSE_FILES+=( -f "$f" )
done

ENV_FILE=""
for candidate in "$PROJECT_DIR/infra/.env" "$PROJECT_DIR/.env" "$HOME/workspace/desifaces-runtime/infra/.env" "$HOME/workspace/desifaces-runtime/.env"; do
  if [[ -f "$candidate" && -s "$candidate" ]]; then ENV_FILE="$candidate"; break; fi
done

COMPOSE_ENV_ARGS=()
if [[ -n "$ENV_FILE" ]]; then
  COMPOSE_ENV_ARGS=(--env-file "$ENV_FILE")
  echo "COMPOSE_ENV_SOURCE=FILE path=$ENV_FILE"
else
  echo "FAIL: canonical DEV compose env unavailable"
  exit 4
fi

COMPOSE=(docker compose --project-directory "$PROJECT_DIR" -p "$PROJECT" "${COMPOSE_ENV_ARGS[@]}" "${COMPOSE_FILES[@]}")
"${COMPOSE[@]}" config -q </dev/null
echo "COMPOSE_INTERPOLATION=PASS"

echo
echo "===== 6. TARGETED AUDIO-ONLY CUTOVER ====="
API_OLD_ID="$(docker inspect "$AUDIO_API" --format '{{.Image}}')"
WORKER_OLD_ID="$(docker inspect "$AUDIO_WORKER" --format '{{.Image}}')"
API_REF="$(docker inspect "$AUDIO_API" --format '{{.Config.Image}}')"
WORKER_REF="$(docker inspect "$AUDIO_WORKER" --format '{{.Config.Image}}')"

API_RB="df-sarvam-narrow-rollback-api:$STAMP"
WORKER_RB="df-sarvam-narrow-rollback-worker:$STAMP"
docker tag "$API_OLD_ID" "$API_RB"
docker tag "$WORKER_OLD_ID" "$WORKER_RB"
docker tag "$AUDIO_IMG" "$API_REF"
docker tag "$AUDIO_IMG" "$WORKER_REF"

health_wait(){
  local c="$1"
  for _ in $(seq 1 60); do
    running="$(docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null || true)"
    health="$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
    if [[ "$running" == "true" && ( "$health" == "healthy" || "$health" == "none" ) ]]; then return 0; fi
    [[ "$health" == "unhealthy" ]] && return 1
    sleep 2
  done
  return 1
}

rollback(){
  echo "===== AUDIO RUNTIME ROLLBACK ====="
  docker tag "$API_RB" "$API_REF" || true
  docker tag "$WORKER_RB" "$WORKER_REF" || true
  "${COMPOSE[@]}" up -d --no-deps --force-recreate --pull never --no-build "$API_SERVICE" "$WORKER_SERVICE" || true
}

set +e
"${COMPOSE[@]}" up -d --no-deps --force-recreate --pull never --no-build "$API_SERVICE" "$WORKER_SERVICE"
RC=$?
[[ "$RC" == "0" ]] && health_wait "$AUDIO_API"; RC1=$?
[[ "$RC" == "0" && "$RC1" == "0" ]] && health_wait "$AUDIO_WORKER"; RC2=$?
set -e

if [[ "${RC:-1}" != "0" || "${RC1:-1}" != "0" || "${RC2:-1}" != "0" ]]; then
  rollback
  echo "SARVAM_NARROW_RUNTIME_CUTOVER=FAIL_ROLLED_BACK"
  exit 5
fi
echo "SARVAM_NARROW_RUNTIME_CUTOVER=PASS"

echo
echo "===== 7. LIVE RESOLVER REGRESSION PROOF ====="
docker exec -i "$AUDIO_API" python - <<'PY'
import asyncio, os
import asyncpg

from app.repos.locale_catalog_repo import LocaleCatalogRepository
from app.repos.locale_context_repo import LocaleContextRepository
from app.repos.tts_catalog_repo import TTSCatalogRepository
from app.services.locale_resolver import LocaleResolver
from app.services.locale_context_resolver import LocaleContextResolver
from app.services.tts_model_resolver import TTSModelResolver
from app.services.tts_voice_resolver import TTSVoiceResolver
from app.services.tts_resolution_planner import TTSResolutionPlanner, TTSResolutionPlanRequest
from app.services.tts_service import TTSService
from app.services.tts_intent_policy import relax_native_style_only_for_eligible_sarvam_voice as relax

async def main():
    pool=await asyncpg.create_pool(os.environ["DATABASE_URL"],min_size=1,max_size=2)
    svc=TTSService(pool)
    planner=TTSResolutionPlanner(
        locale_resolver=LocaleResolver(LocaleCatalogRepository(pool)),
        context_resolver=LocaleContextResolver(LocaleContextRepository(pool)),
        model_resolver=TTSModelResolver(TTSCatalogRepository(pool)),
        voice_resolver=TTSVoiceResolver(TTSCatalogRepository(pool)),
    )

    # Sarvam voice + supported locale is the ONLY relaxed path.
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale="hi-IN")
    assert eligible is True
    req_style=bool("Conversational") and not relax("Conversational",sarvam_voice_eligible=eligible)
    assert req_style is False
    p=await planner.resolve(TTSResolutionPlanRequest(
        requested_locale="hi-IN",text_length=100,output_format="mp3",
        requested_voice="shubh",requested_gender="male",
        requires_style=req_style,requires_emotion=False,requires_streaming=False,
    ))
    assert p.provider_code=="sarvam" and p.model_code=="bulbul_v3"
    print("SARVAM_HI_IN_CONVERSATIONAL_RESOLUTION=PASS")

    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale="pa-IN")
    assert eligible is True
    req_style=bool("Narration") and not relax("Narration",sarvam_voice_eligible=eligible)
    p=await planner.resolve(TTSResolutionPlanRequest(
        requested_locale="pa-IN",text_length=100,output_format="mp3",
        requested_voice="shubh",requested_gender="male",
        requires_style=req_style,requires_emotion=False,requires_streaming=False,
    ))
    assert p.provider_code=="sarvam" and p.model_code=="bulbul_v3"
    print("SARVAM_PA_IN_NARRATION_RESOLUTION=PASS")

    # Existing non-Sarvam behavior remains strict.
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="en-IN-AaravNeural",target_locale="en-IN")
    assert eligible is False
    req_style=bool("Conversational") and not relax("Conversational",sarvam_voice_eligible=eligible)
    assert req_style is True
    p=await planner.resolve(TTSResolutionPlanRequest(
        requested_locale="en-IN",text_length=100,output_format="mp3",
        requested_voice="en-IN-AaravNeural",requested_gender="male",
        requires_style=req_style,requires_emotion=False,requires_streaming=False,
    ))
    assert p.provider_code=="azure"
    print("AZURE_EXISTING_STYLE_SEMANTICS=PASS")

    # Sarvam must not be enabled for unrelated global locale.
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale="en-US")
    assert eligible is False
    print("SARVAM_GLOBAL_EXCLUSION=PASS")

    # Character remains strict. Existing planner semantics may fall back from
    # an incompatible explicit voice to a compatible provider/voice; preserve
    # that behavior rather than requiring a terminal failure.
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale="hi-IN")
    assert eligible is True
    req_style=bool("Character") and not relax("Character",sarvam_voice_eligible=eligible)
    assert req_style is True
    p=await planner.resolve(TTSResolutionPlanRequest(
        requested_locale="hi-IN",text_length=100,output_format="mp3",
        requested_voice="shubh",requested_gender="male",
        requires_style=req_style,requires_emotion=False,requires_streaming=False,
    ))
    assert p.provider_code != "sarvam",(p.provider_code,p.model_code,p.voice_name)
    print(f"CHARACTER_EXISTING_FALLBACK_BEHAVIOR=PASS provider={p.provider_code} model={p.model_code}")

    await pool.close()

asyncio.run(main())
PY

echo
echo "============================================================"
echo "SARVAM_SUPPORTED_LOCALES_ONLY=PASS"
echo "NON_SARVAM_AUDIO_BEHAVIOR_UNCHANGED=PASS"
echo "AUDIO_FEATURE_REGRESSION_GATE=PASS"
echo "db_mutation=NONE"
echo "face_video_workflows=FROZEN"
echo "production=UNTOUCHED"
echo "SARVAM_NARROW_ENABLEMENT=PASS"
echo "============================================================"
