#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }
TARGET_SHA="${TARGET_SHA:?TARGET_SHA is required}"
SHORT="${TARGET_SHA:0:12}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

echo "============================================================"
echo " desifaces DEV — AUDIO LOCATION ROUTING + SARVAM ALIGNMENT"
echo "============================================================"
echo "target_sha=$TARGET_SHA"
echo "scope=SVC_AUDIO_ROUTING_AND_AUDIO_COGS_ONLY"
echo "face_video_workflows=FROZEN"
echo "production=UNTOUCHED"

REPO=""
for p in "$HOME/workspace/desifaces-v3" "$HOME/workspace/desifaces_backend" "$HOME/workspace/desifaces-backend"; do
  if [[ -d "$p/.git" ]]; then REPO="$p"; break; fi
done
[[ -n "$REPO" ]] || { echo "FAIL: backend repo not found"; exit 2; }

WT="/tmp/df-audio-sarvam-$SHORT"
rm -rf "$WT"
git -C "$REPO" fetch origin "$TARGET_SHA" >/dev/null 2>&1 || true
git -C "$REPO" worktree add --detach "$WT" "$TARGET_SHA" >/dev/null
cleanup(){ git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT

MIG="$WT/migrations/2026_09_29_dev_audio_location_routing_sarvam_cogs.sql"
AUDIT="$WT/scripts/ops/certify-dev-narrow-launch-sku-cogs-v4.sh"
[[ -f "$MIG" && -f "$AUDIT" ]] || { echo "FAIL: pinned migration/audit missing"; exit 2; }

echo
echo "===== 1. SOURCE CONTRACT ====="
grep -q 'requires_provider_native_style' "$WT/services/svc-audio/app/app/services/tts_service.py"
grep -q '"conversational"' "$WT/services/svc-audio/app/app/services/tts_intent_policy.py"
grep -q '"narration"' "$WT/services/svc-audio/app/app/services/tts_intent_policy.py"
grep -q 'return raw not in _PRODUCT_INTENT_WITHOUT_NATIVE_STYLE_REQUIREMENT' "$WT/services/svc-audio/app/app/services/tts_intent_policy.py"
grep -q 'south_asia_specialist' "$MIG"
echo "AUDIO_ROUTING_SOURCE_CONTRACT=PASS"

echo
echo "===== 2. BUILD AUDIO CANDIDATE ====="
AUDIO_IMG="df-audio-sarvam:$SHORT"
docker build -f "$WT/services/svc-audio/app/Dockerfile" -t "$AUDIO_IMG" "$WT" >/tmp/df-audio-sarvam-build.log 2>&1
echo "AUDIO_BUILD=PASS image=$AUDIO_IMG"

echo
echo "===== 3. IN-IMAGE CERTIFICATION ====="
docker run --rm "$AUDIO_IMG" python -m py_compile   /app/app/services/tts_intent_policy.py   /app/app/services/tts_service.py   /app/app/services/tts_model_resolver.py   /app/app/services/tts_resolution_planner.py   /app/app/services/sarvam_tts_adapter.py

docker run --rm -i "$AUDIO_IMG" python - <<'PY'
from app.services.tts_intent_policy import requires_provider_native_style
assert requires_provider_native_style(None) is False
assert requires_provider_native_style("Conversational") is False
assert requires_provider_native_style("Narration") is False
assert requires_provider_native_style("Character") is True
assert requires_provider_native_style("cheerful") is True
print("AUDIO_PRODUCT_INTENT_POLICY=PASS")
PY
echo "AUDIO_IN_IMAGE_CERTIFICATION=PASS"

echo
echo "===== 4. DB PREFLIGHT — ROLLBACK ONLY ====="
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

PREFLIGHT="/tmp/df-audio-sarvam-preflight.sql"
{
  echo "BEGIN;"
  sed -e '/^[[:space:]]*BEGIN;[[:space:]]*$/d' -e '/^[[:space:]]*COMMIT;[[:space:]]*$/d' "$MIG"
  echo "ROLLBACK;"
} > "$PREFLIGHT"
"${PSQL[@]}" < "$PREFLIGHT" >/tmp/df-audio-sarvam-preflight.log
echo "AUDIO_SARVAM_MIGRATION_PREFLIGHT=PASS"

echo
echo "===== 5. SNAPSHOT ====="
SNAP="/tmp/desifaces-audio-sarvam-before-$STAMP.txt"
"${PSQL[@]}" -P pager=off -c "
select 'PROVIDER' kind,p.provider_code key,p.routing_enabled::text value,p.meta_json::text detail
from public.tts_providers p where p.provider_code in ('azure','elevenlabs','sarvam')
union all
select 'MODEL',m.provider_code||':'||m.model_code,m.routing_enabled::text,m.meta_json::text
from public.tts_provider_models m where m.provider_code in ('azure','elevenlabs','sarvam')
union all
select 'PRICE_COGS',c.sku_code||':'||c.component_code,c.variable_cost_money::text,c.metadata_json::text
from public.pricing_sku_costs c
where c.is_active=true and c.sku_code in ('AUDIO_TTS_1K_CHARS','AUDIO_MULTI_PERSON')
order by 1,2;
" > "$SNAP"
echo "AUDIO_SARVAM_SNAPSHOT=$SNAP"

echo
echo "===== 6. APPLY AUDIO ROUTING / COGS MIGRATION ====="
"${PSQL[@]}" < "$MIG" >/tmp/df-audio-sarvam-apply.log
echo "AUDIO_SARVAM_DB_ALIGNMENT=PASS"

echo
echo "===== 7. CANONICAL COMPOSE ENV ====="
PROJECT="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
PROJECT_DIR="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
CONFIG_FILES="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
API_SERVICE="$(docker inspect "$AUDIO_API" --format '{{index .Config.Labels "com.docker.compose.service"}}')"
WORKER_SERVICE="$(docker inspect "$AUDIO_WORKER" --format '{{index .Config.Labels "com.docker.compose.service"}}')"
[[ -n "$PROJECT" && -n "$PROJECT_DIR" && -d "$PROJECT_DIR" && -n "$CONFIG_FILES" && -n "$API_SERVICE" && -n "$WORKER_SERVICE" ]] || {
  echo "FAIL: compose ownership metadata incomplete"; exit 3;
}

IFS=',' read -r -a CFG_ARR <<< "$CONFIG_FILES"
COMPOSE_FILES=()
for f in "${CFG_ARR[@]}"; do
  [[ "$f" = /* ]] || f="$PROJECT_DIR/$f"
  [[ -f "$f" ]] || { echo "FAIL: compose file missing: $f"; exit 3; }
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
  echo "COMPOSE_ENV_SOURCE=RUNNING_CONTAINERS"
  while IFS= read -r c; do
    while IFS= read -r kv; do
      key="${kv%%=*}"; val="${kv#*=}"
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      if [[ -z "${!key+x}" ]]; then export "$key=$val"; fi
    done < <(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}')
  done < <(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}')
fi

COMPOSE=(docker compose --project-directory "$PROJECT_DIR" -p "$PROJECT" "${COMPOSE_ENV_ARGS[@]}" "${COMPOSE_FILES[@]}")
"${COMPOSE[@]}" config -q </dev/null
echo "COMPOSE_INTERPOLATION=PASS"

echo
echo "===== 8. TARGETED AUDIO CUTOVER ====="
API_OLD_ID="$(docker inspect "$AUDIO_API" --format '{{.Image}}')"
WORKER_OLD_ID="$(docker inspect "$AUDIO_WORKER" --format '{{.Image}}')"
API_REF="$(docker inspect "$AUDIO_API" --format '{{.Config.Image}}')"
WORKER_REF="$(docker inspect "$AUDIO_WORKER" --format '{{.Config.Image}}')"
[[ "$API_REF" != *@* && "$WORKER_REF" != *@* ]] || { echo "FAIL: digest-only live ref"; exit 4; }

API_RB="df-audio-sarvam-rollback-api:$STAMP"
WORKER_RB="df-audio-sarvam-rollback-worker:$STAMP"
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
if [[ "$RC" == "0" ]]; then
  health_wait "$AUDIO_API"; RC=$?
fi
if [[ "$RC" == "0" ]]; then
  health_wait "$AUDIO_WORKER"; RC=$?
fi
set -e
if [[ "$RC" != "0" ]]; then
  rollback
  echo "AUDIO_SARVAM_RUNTIME_CUTOVER=FAIL_ROLLED_BACK"
  exit 5
fi
echo "AUDIO_SARVAM_RUNTIME_CUTOVER=PASS"

echo
echo "===== 9. LIVE LOCATION / STYLE ROUTING CERTIFICATION ====="
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
from app.services.tts_intent_policy import requires_provider_native_style

SARVAM_LOCALES=["en-IN","hi-IN","bn-IN","ta-IN","te-IN","kn-IN","ml-IN","mr-IN","gu-IN","pa-IN","or-IN"]
GLOBAL_CHECKS=["en-US","de-DE"]

async def main():
    pool=await asyncpg.create_pool(os.environ["DATABASE_URL"],min_size=1,max_size=2)
    planner=TTSResolutionPlanner(
        locale_resolver=LocaleResolver(LocaleCatalogRepository(pool)),
        context_resolver=LocaleContextResolver(LocaleContextRepository(pool)),
        model_resolver=TTSModelResolver(TTSCatalogRepository(pool)),
        voice_resolver=TTSVoiceResolver(TTSCatalogRepository(pool)),
    )

    for locale in SARVAM_LOCALES:
        for style in ("Conversational","Narration"):
            p=await planner.resolve(TTSResolutionPlanRequest(
                requested_locale=locale,
                text_length=120,
                output_format="mp3",
                requested_voice="shubh",
                requested_gender="male",
                requires_style=requires_provider_native_style(style),
                requires_emotion=False,
                requires_streaming=False,
            ))
            assert p.provider_code=="sarvam",(locale,style,p.provider_code,p.model_code,p.voice_name)
            assert p.model_code=="bulbul_v3",(locale,style,p.model_code)
            print(f"SARVAM_SUPPORTED_LOCALE=PASS locale={locale} style={style} voice={p.voice_name}")

    for locale in GLOBAL_CHECKS:
        p=await planner.resolve(TTSResolutionPlanRequest(
            requested_locale=locale,
            text_length=120,
            output_format="mp3",
            requested_voice=None,
            requested_gender=None,
            requires_style=requires_provider_native_style("Conversational"),
            requires_emotion=False,
            requires_streaming=False,
        ))
        assert p.provider_code!="sarvam",(locale,p.provider_code,p.model_code)
        print(f"GLOBAL_LOCALE_NOT_SARVAM=PASS locale={locale} provider={p.provider_code} model={p.model_code}")

    character=requires_provider_native_style("Character")
    assert character is True
    print("CHARACTER_NATIVE_STYLE_GATE=PASS")
    await pool.close()

asyncio.run(main())
PY

echo
echo "===== 10. PROVIDER COST MASTERDATA ====="
"${PSQL[@]}" -P pager=off -c "
select provider_code,model_code,unit_type,unit_size,unit_cost,currency,effective_from
from public.tts_provider_cost_profiles
where is_enabled=true
  and (
    (provider_code='azure' and model_code='speech_standard_neural')
    or (provider_code='elevenlabs' and model_code in ('eleven_flash_v2_5','eleven_multilingual_v2','eleven_v3'))
    or (provider_code='sarvam' and model_code='bulbul_v3')
  )
order by provider_code,model_code;
"

echo
echo "===== 11. COMMERCIAL REGRESSION GATE ====="
set +e
bash "$AUDIT" 2>&1 | tee /tmp/df-audio-sarvam-commercial-certification.log
AUDIT_RC=${PIPESTATUS[0]}
set -e

echo
echo "============================================================"
echo " AUDIO SARVAM ALIGNMENT FINAL"
echo "============================================================"
echo "db_alignment=PASS"
echo "runtime_cutover=PASS"
echo "routing_certification=PASS"
echo "commercial_audit_rc=$AUDIT_RC"
echo "face_video_workflows=FROZEN"
echo "production=UNTOUCHED"

if [[ "$AUDIT_RC" != "0" ]]; then
  echo "AUDIO_SARVAM_ALIGNMENT=FAIL_CERTIFICATION"
  exit "$AUDIT_RC"
fi

echo "AUDIO_SARVAM_ALIGNMENT=PASS"
echo "============================================================"
