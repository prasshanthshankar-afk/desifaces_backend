#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: desifaces-dev required"; exit 2; }

AUDIO_API="${AUDIO_API:-df-svc-audio}"
AUDIO_WORKER="${AUDIO_WORKER:-df-svc-audio-worker}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

for c in "$AUDIO_API" "$AUDIO_WORKER" "$DB_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c"; exit 2; }
done

DB_URL="$(docker exec "$AUDIO_API" sh -lc 'printf "%s" "$DATABASE_URL"')"
[[ -n "$DB_URL" ]] || { echo "FAIL: svc-audio DATABASE_URL unavailable"; exit 2; }
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — SARVAM AUDIO ROUTING TRUTH"
echo " READ ONLY / NO SYNTHESIS / NO DB MUTATION"
echo "============================================================"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "runtime_mutation=NONE"
echo "production=UNTOUCHED"

echo
echo "===== 1. LIVE SOURCE CONTRACT ====="
docker exec "$AUDIO_API" python - <<'PY'
from pathlib import Path

registry=Path("/app/app/services/tts_provider_registry.py").read_text()
adapter=Path("/app/app/services/sarvam_tts_adapter.py").read_text()
service=Path("/app/app/services/tts_service.py").read_text()
catalog=Path("/app/app/repos/tts_catalog_repo.py").read_text()
route=Path("/app/app/api/routes/catalog.py").read_text()

assert '"sarvam": sarvam' in registry
assert 'return "sarvam"' in adapter
assert '/text-to-speech' in adapter
assert 'self.provider_executor = TTSProviderExecutor()' in service
assert 'await self.provider_executor.synthesize(' in service
assert 'requires_style=bool(style)' in service
assert 'requires_emotion=bool(emotion)' in service
assert 'p.routing_enabled = true' in catalog.lower()
assert 'm.routing_enabled = true' in catalog.lower()
assert '_executable_model_for_voice_sql' in route

print("SARVAM_ADAPTER_PRESENT=PASS")
print("PROVIDER_NEUTRAL_EXECUTION_WIRED=PASS")
print("STYLE_ELIGIBILITY_GATE_PRESENT=YES")
print("EMOTION_ELIGIBILITY_GATE_PRESENT=YES")
print("CATALOG_REQUIRES_ROUTED_PROVIDER_MODEL=YES")
PY

echo
echo "===== 2. CREDENTIAL PRESENCE — VALUES NEVER PRINTED ====="
for c in "$AUDIO_API" "$AUDIO_WORKER"; do
  echo "--- $c ---"
  for k in SARVAM_API_KEY AZURE_SPEECH_KEY AZURE_SPEECH_REGION; do
    present="$(docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' |
      awk -F= -v key="$k" '$1==key { if (length(substr($0,length(key)+2))>0) found=1 } END { print found ? "PRESENT" : "MISSING" }')"
    echo "$k=$present"
  done
done

echo
echo "===== 3. PROVIDER + MODEL ROUTING STATE ====="
"${PSQL[@]}" -P pager=off -c "
select
  p.provider_code,
  p.adapter_key,
  p.is_enabled provider_enabled,
  p.routing_enabled provider_routing_enabled,
  p.meta_json->>'role' provider_role,
  m.model_code,
  m.provider_model_id,
  m.quality_class,
  m.is_enabled model_enabled,
  m.routing_enabled model_routing_enabled,
  m.supports_styles,
  m.supports_emotions,
  m.supports_streaming,
  m.max_input_chars
from public.tts_providers p
join public.tts_provider_models m on m.provider_code=p.provider_code
where p.provider_code in ('azure','sarvam','elevenlabs')
order by p.provider_code,m.model_code;
"

echo
echo "===== 4. DEFAULT ROUTING POLICY ====="
"${PSQL[@]}" -P pager=off -c "
select
  policy_code,
  require_approved_capability,
  require_approved_quality,
  allow_provider_fallback,
  is_default,
  is_enabled,
  meta_json
from public.tts_routing_policies
where is_default=true or policy_code='global_quality_first'
order by is_default desc,policy_code;
"

echo
echo "===== 5. SARVAM LANGUAGE / LOCALE CAPABILITY ====="
"${PSQL[@]}" -P pager=off -c "
select
  mlc.locale,
  mlc.provider_locale_code,
  mlc.is_enabled,
  mlc.is_approved,
  count(distinct vl.voice_id) filter (
    where vl.is_enabled=true and vl.is_approved=true
  ) eligible_voice_locale_rows,
  count(distinct vl.voice_id) filter (
    where vl.is_enabled=true and vl.is_approved=true and vl.is_recommended=true
  ) recommended_voice_rows,
  max(vl.selection_priority) max_selection_priority
from public.tts_model_locale_capabilities mlc
left join public.tts_voices v
  on v.provider=mlc.provider_code
left join public.tts_voice_model_capabilities vm
  on vm.voice_id=v.id
 and vm.provider_code=mlc.provider_code
 and vm.model_code=mlc.model_code
 and vm.is_enabled=true
 and vm.is_approved=true
left join public.tts_voice_locale_capabilities vl
  on vl.voice_id=v.id
 and vl.locale=mlc.locale
where mlc.provider_code='sarvam'
  and mlc.model_code='bulbul_v3'
group by mlc.locale,mlc.provider_locale_code,mlc.is_enabled,mlc.is_approved
order by mlc.locale;
"

echo
echo "===== 6. WHAT THE PUBLIC AUDIO VOICE CATALOG CAN EXPOSE ====="
"${PSQL[@]}" -P pager=off -c "
with locales(locale,language_code) as (
  values
    ('en-IN','en'),('hi-IN','hi'),('bn-IN','bn'),('ta-IN','ta'),
    ('te-IN','te'),('kn-IN','kn'),('ml-IN','ml'),('mr-IN','mr'),
    ('gu-IN','gu'),('pa-IN','pa'),('or-IN','or')
),
eligible as (
  select distinct
    l.locale,
    v.provider,
    v.id
  from locales l
  join public.tts_voice_locale_capabilities vl
    on vl.locale=l.locale and vl.is_enabled=true and vl.is_approved=true
  join public.tts_voices v on v.id=vl.voice_id
  join public.tts_voice_model_capabilities vm
    on vm.voice_id=v.id
   and vm.provider_code=v.provider
   and vm.is_enabled=true
   and vm.is_approved=true
  join public.tts_provider_models m
    on m.provider_code=vm.provider_code
   and m.model_code=vm.model_code
   and m.is_enabled=true
   and m.routing_enabled=true
  join public.tts_providers p
    on p.provider_code=vm.provider_code
   and p.is_enabled=true
   and p.routing_enabled=true
  where exists (
    select 1
    from public.tts_model_locale_capabilities mlc
    where mlc.provider_code=vm.provider_code
      and mlc.model_code=vm.model_code
      and mlc.locale=l.locale
      and mlc.is_enabled=true
      and mlc.is_approved=true
    union all
    select 1
    from public.tts_model_language_capabilities mlng
    where mlng.provider_code=vm.provider_code
      and mlng.model_code=vm.model_code
      and mlng.language_code=l.language_code
      and mlng.is_enabled=true
      and mlng.is_approved=true
  )
)
select locale,provider,count(*) voices
from eligible
group by locale,provider
order by locale,provider;
"

echo
echo "===== 7. ACTUAL RESOLVER DRY-RUN — NO TTS PROVIDER CALL ====="
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

LOCALES=["en-IN","hi-IN","bn-IN","ta-IN","te-IN","kn-IN","ml-IN","mr-IN","gu-IN","pa-IN","or-IN"]

async def main():
    pool=await asyncpg.create_pool(os.environ["DATABASE_URL"],min_size=1,max_size=2)
    planner=TTSResolutionPlanner(
        locale_resolver=LocaleResolver(LocaleCatalogRepository(pool)),
        context_resolver=LocaleContextResolver(LocaleContextRepository(pool)),
        model_resolver=TTSModelResolver(TTSCatalogRepository(pool)),
        voice_resolver=TTSVoiceResolver(TTSCatalogRepository(pool)),
    )
    for locale in LOCALES:
        for scenario,style,emotion in [
            ("AUTO_NO_STYLE",False,False),
            ("AUDIO_STUDIO_CONVERSATIONAL",True,False),
            ("DIALOGUE_WITH_EMOTION",False,True),
        ]:
            try:
                p=await planner.resolve(TTSResolutionPlanRequest(
                    requested_locale=locale,
                    text_length=120,
                    output_format="mp3",
                    requested_voice=None,
                    requested_gender=None,
                    requires_style=style,
                    requires_emotion=emotion,
                    requires_streaming=False,
                ))
                print(
                    f"RESOLVE locale={locale} scenario={scenario} "
                    f"provider={p.provider_code} model={p.model_code} "
                    f"adapter={p.adapter_key} voice={p.voice_name}"
                )
            except Exception as e:
                print(f"RESOLVE locale={locale} scenario={scenario} ERROR={type(e).__name__}:{str(e)[:240]}")
    await pool.close()

asyncio.run(main())
PY

echo
echo "===== 8. RECENT ACTUAL AUDIO GENERATION PROVIDERS — 30 DAYS ====="
"${PSQL[@]}" -P pager=off -c "
select
  coalesce(
    a.meta_json->>'provider_code',
    sj.payload_json->'tts_meta'->>'provider_code',
    'unknown'
  ) provider_code,
  coalesce(
    a.meta_json->>'model_code',
    sj.payload_json->'tts_meta'->>'model_code',
    'unknown'
  ) model_code,
  coalesce(
    a.meta_json->>'target_locale',
    sj.payload_json->>'target_locale',
    'unknown'
  ) target_locale,
  count(*) generated_audio,
  max(a.created_at) last_seen
from public.artifacts a
join public.studio_jobs sj on sj.id=a.job_id
where a.kind='audio'
  and sj.studio_type='audio'
  and a.created_at>=now()-interval '30 days'
group by 1,2,3
order by generated_audio desc,provider_code,model_code,target_locale;
"

RECENT_SARVAM="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.artifacts a
join public.studio_jobs sj on sj.id=a.job_id
where a.kind='audio'
  and sj.studio_type='audio'
  and a.created_at>=now()-interval '30 days'
  and lower(coalesce(
    a.meta_json->>'provider_code',
    sj.payload_json->'tts_meta'->>'provider_code',
    ''
  ))='sarvam';
")"

echo
echo "===== 9. SUMMARY ====="
PROVIDER_ROUTING="$("${PSQL[@]}" -Atq -c "select coalesce(routing_enabled,false) from public.tts_providers where provider_code='sarvam';")"
MODEL_ROUTING="$("${PSQL[@]}" -Atq -c "select coalesce(routing_enabled,false) from public.tts_provider_models where provider_code='sarvam' and model_code='bulbul_v3';")"

case "$PROVIDER_ROUTING" in
  t|true) echo "SARVAM_PROVIDER_ROUTING=ENABLED" ;;
  *) echo "SARVAM_PROVIDER_ROUTING=DISABLED" ;;
esac
case "$MODEL_ROUTING" in
  t|true) echo "SARVAM_MODEL_ROUTING=ENABLED" ;;
  *) echo "SARVAM_MODEL_ROUTING=DISABLED" ;;
esac
echo "SARVAM_RECENT_GENERATIONS_30D=${RECENT_SARVAM:-0}"
echo "SARVAM_RUNTIME_AUDIT=COMPLETE"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "runtime_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
