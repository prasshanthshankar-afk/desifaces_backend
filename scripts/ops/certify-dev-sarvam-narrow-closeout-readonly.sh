#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

AUDIO_API="${AUDIO_API:-df-svc-audio}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

for c in "$AUDIO_API" "$DB_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing $c"; exit 2; }
done

echo "============================================================"
echo " desifaces DEV — SARVAM NARROW CLOSEOUT"
echo " READ ONLY / NO PROVIDER CALL / NO MUTATION"
echo "============================================================"
echo "production=UNTOUCHED"
echo "db_mutation=NONE"
echo "runtime_mutation=NONE"

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

    # Narrow Sarvam compatibility paths.
    for locale,style in [("hi-IN","Conversational"),("pa-IN","Narration")]:
        eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale=locale)
        assert eligible is True,(locale,"sarvam eligibility")
        requires_style=bool(style) and not relax(style,sarvam_voice_eligible=eligible)
        assert requires_style is False,(locale,style,"relax")
        p=await planner.resolve(TTSResolutionPlanRequest(
            requested_locale=locale,text_length=100,output_format="mp3",
            requested_voice="shubh",requested_gender="male",
            requires_style=requires_style,requires_emotion=False,requires_streaming=False,
        ))
        assert p.provider_code=="sarvam" and p.model_code=="bulbul_v3",(locale,p)
        print(f"SARVAM_NARROW_ROUTE=PASS locale={locale} style={style} provider={p.provider_code} model={p.model_code}")

    # Existing Azure path stays strict.
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="en-IN-AaravNeural",target_locale="en-IN")
    assert eligible is False
    requires_style=bool("Conversational") and not relax("Conversational",sarvam_voice_eligible=eligible)
    assert requires_style is True
    p=await planner.resolve(TTSResolutionPlanRequest(
        requested_locale="en-IN",text_length=100,output_format="mp3",
        requested_voice="en-IN-AaravNeural",requested_gender="male",
        requires_style=requires_style,requires_emotion=False,requires_streaming=False,
    ))
    assert p.provider_code=="azure",(p.provider_code,p.model_code,p.voice_name)
    print("AZURE_EXISTING_STYLE_SEMANTICS=PASS")

    # Sarvam does not leak to unrelated global locale.
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale="en-US")
    assert eligible is False
    print("SARVAM_GLOBAL_EXCLUSION=PASS")

    # Existing stale/incompatible explicit-voice behavior is fallback, not failure.
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale="hi-IN")
    assert eligible is True
    requires_style=bool("Character") and not relax("Character",sarvam_voice_eligible=eligible)
    assert requires_style is True
    p=await planner.resolve(TTSResolutionPlanRequest(
        requested_locale="hi-IN",text_length=100,output_format="mp3",
        requested_voice="shubh",requested_gender="male",
        requires_style=requires_style,requires_emotion=False,requires_streaming=False,
    ))
    assert p.provider_code != "sarvam",(p.provider_code,p.model_code,p.voice_name)
    print(f"CHARACTER_EXISTING_FALLBACK_BEHAVIOR=PASS provider={p.provider_code} model={p.model_code}")

    await pool.close()

asyncio.run(main())
PY

DB_URL="$(docker exec "$AUDIO_API" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"

CAPS="$(docker exec -i "$DB_CONTAINER" psql -X -Atq -U "$DB_USER" -d "$DB_NAME" -c "
select string_agg(locale,',' order by locale)
from public.tts_model_locale_capabilities
where provider_code='sarvam' and model_code='bulbul_v3'
  and is_enabled=true and is_approved=true;
")"
EXPECTED="bn-IN,en-IN,gu-IN,hi-IN,kn-IN,ml-IN,mr-IN,or-IN,pa-IN,ta-IN,te-IN"
[[ "$CAPS" == "$EXPECTED" ]] || { echo "FAIL: Sarvam locale capability drift actual=$CAPS"; exit 3; }

echo "SARVAM_SUPPORTED_LOCALE_SET=PASS"
echo "NON_SARVAM_AUDIO_BEHAVIOR_UNCHANGED=PASS"
echo "SARVAM_NARROW_CLOSEOUT=PASS"
echo "============================================================"
