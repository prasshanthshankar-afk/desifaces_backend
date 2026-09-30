#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"
FACE_API="${FACE_API:-df-svc-face}"
AUDIO_API="${AUDIO_API:-df-svc-audio}"
FUSION_API="${FUSION_API:-df-svc-fusion}"
FUSION_EXT="${FUSION_EXT:-df-svc-fusion-extension}"
DIRECTOR_API="${DIRECTOR_API:-df-svc-director}"

echo "============================================================"
echo " desifaces DEV — LAUNCH GENERATION + PRICING MATRIX"
echo " READ ONLY / REUSES ACTUAL GENERATION EVIDENCE"
echo "============================================================"
echo "production=UNTOUCHED"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"

for c in "$DB_CONTAINER" "$PRICING_CONTAINER" "$FACE_API" "$AUDIO_API" "$FUSION_API" "$FUSION_EXT" "$DIRECTOR_API"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: required container missing $c"; exit 2; }
done

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo
echo "===== 1. RUNTIME HEALTH ====="
health_container() {
  local c="$1"
  local running health
  running="$(docker inspect "$c" --format '{{.State.Running}}')"
  health="$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
  [[ "$running" == "true" ]] || { echo "FAIL: $c not running"; exit 3; }
  [[ "$health" == "healthy" || "$health" == "none" ]] || { echo "FAIL: $c health=$health"; exit 3; }
  echo "RUNTIME_HEALTH=PASS container=$c health=$health"
}
for c in "$FACE_API" "$AUDIO_API" "$FUSION_API" "$FUSION_EXT" "$DIRECTOR_API" "$PRICING_CONTAINER"; do
  health_container "$c"
done

echo
echo "===== 2. COMMERCIAL CONTRACT ====="
AUDIT=""
for p in   "$HOME/workspace/desifaces-v3/scripts/ops/certify-dev-narrow-launch-sku-cogs-v4.sh"   "$HOME/workspace/desifaces_backend/scripts/ops/certify-dev-narrow-launch-sku-cogs-v4.sh"   "$HOME/workspace/desifaces-runtime/scripts/ops/certify-dev-narrow-launch-sku-cogs-v4.sh"; do
  [[ -f "$p" ]] && { AUDIT="$p"; break; }
done
if [[ -z "$AUDIT" ]]; then
  echo "FAIL: V4 commercial audit script not found in workspace"
  exit 4
fi
bash "$AUDIT"
echo "LAUNCH_COMMERCIAL_CONTRACT=PASS"

echo
echo "===== 3. SARVAM NARROW AUDIO CONTRACT ====="
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
    for locale,style in [("hi-IN","Conversational"),("pa-IN","Narration")]:
        eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale=locale)
        assert eligible
        requires_style=bool(style) and not relax(style,sarvam_voice_eligible=eligible)
        p=await planner.resolve(TTSResolutionPlanRequest(
            requested_locale=locale,text_length=80,output_format="mp3",
            requested_voice="shubh",requested_gender="male",
            requires_style=requires_style,requires_emotion=False,requires_streaming=False,
        ))
        assert p.provider_code=="sarvam" and p.model_code=="bulbul_v3",(locale,p)
        print(f"SARVAM_ROUTE=PASS locale={locale} style={style}")
    eligible=await svc._is_explicit_sarvam_voice_eligible(voice="shubh",target_locale="en-US")
    assert not eligible
    print("SARVAM_GLOBAL_EXCLUSION=PASS")
    await pool.close()
asyncio.run(main())
PY

echo
echo "===== 4. PRICING COMMIT EVIDENCE BY VARIANT ====="
"${PSQL[@]}" -P pager=off -c "
with wanted(code,category) as (
  values
    ('FACE_T2I','FACE_SINGLE_T2I'),
    ('FACE_I2I','FACE_SINGLE_I2I'),
    ('FACE_MULTI_PERSON','FACE_MULTI_T2I'),
    ('FACE_MULTI_PERSON_I2I','FACE_MULTI_I2I'),
    ('AUDIO_TTS','AUDIO_SINGLE'),
    ('AUDIO_MULTI_PERSON','AUDIO_MULTI'),
    ('FUSION_TALKING_VIDEO','VIDEO_SINGLE'),
    ('FUSION_MULTI_PERSON','VIDEO_MULTI_GROUP')
),
evidence as (
  select
    coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code','') code,
    count(*) filter (where r.status='committed') committed_count,
    max(r.created_at) filter (where r.status='committed') last_committed,
    max(nullif(r.quote_json->>'final_charged_credits','')::numeric)
      filter (where r.status='committed') last_or_max_final_credits,
    bool_or(
      r.status='committed'
      and coalesce((r.quote_json->'economics'->>'has_costs_complete')::boolean,false)=true
      and r.quote_json->'economics'->>'cogs_money_final' is not null
      and jsonb_array_length(coalesce(r.quote_json->'economics'->'missing_cost_skus','[]'::jsonb))=0
    ) economics_complete
  from public.pricing_credit_reservations r
  where r.created_at>=now()-interval '45 days'
  group by 1
)
select
  w.category,
  w.code variant_code,
  coalesce(e.committed_count,0) committed_count,
  e.last_committed,
  e.last_or_max_final_credits,
  coalesce(e.economics_complete,false) economics_complete,
  case
    when coalesce(e.committed_count,0)>0 then 'PASS'
    else 'MISSING'
  end pricing_commit_gate
from wanted w
left join evidence e on e.code=w.code
order by w.category;
"

echo
echo "===== 5. SUCCESSFUL MEDIA EVIDENCE ====="
"${PSQL[@]}" -P pager=off -c "
with media as (
  select
    sj.id job_id,
    sj.created_at,
    sj.status,
    sj.studio_type,
    sj.payload_json,
    sj.meta_json job_meta,
    a.kind,
    a.meta_json artifact_meta,
    a.bytes,
    coalesce(a.meta_json->>'asset_class',sj.meta_json->>'asset_class',sj.payload_json->>'asset_class','') asset_class,
    coalesce(a.meta_json->>'provider_code',sj.payload_json->'tts_meta'->>'provider_code','') provider_code,
    coalesce(a.meta_json->>'model_code',sj.payload_json->'tts_meta'->>'model_code','') model_code
  from public.studio_jobs sj
  join public.artifacts a on a.job_id=sj.id
  where sj.status='succeeded'
    and sj.created_at>=now()-interval '45 days'
    and coalesce(a.bytes,0)>0
)
select
  case
    when kind='image' and asset_class='group_photo' then 'GROUP_PHOTO_IMAGE'
    when kind='image' and asset_class='multi_person_face' then 'MULTI_PERSON_FACE_IMAGE'
    when kind='image' then 'FACE_IMAGE'
    when kind='audio' and asset_class='group_audio' then 'GROUP_AUDIO'
    when kind='audio' and asset_class='multi_person_audio' then 'MULTI_PERSON_AUDIO'
    when kind='audio' then 'AUDIO'
    when kind='video' and asset_class='group_video' then 'GROUP_VIDEO'
    when kind='video' and asset_class='multi_person_video' then 'MULTI_PERSON_VIDEO'
    when kind='video' then 'VIDEO'
    else upper(kind)
  end media_category,
  count(*) artifacts,
  max(created_at) last_success,
  string_agg(distinct nullif(provider_code,''),',' order by nullif(provider_code,'')) providers,
  string_agg(distinct nullif(model_code,''),',' order by nullif(model_code,'')) models
from media
group by 1
order by 1;
"

echo
echo "===== 6. END-TO-END CATEGORY MATRIX ====="
"${PSQL[@]}" -P pager=off -c "
with wanted(category,variant_code,kind,asset_class,allow_generic) as (
  values
    ('FACE_SINGLE_T2I','FACE_T2I','image','',true),
    ('FACE_SINGLE_I2I','FACE_I2I','image','',true),
    ('FACE_MULTI_T2I','FACE_MULTI_PERSON','image','multi_person_face',false),
    ('FACE_MULTI_I2I','FACE_MULTI_PERSON_I2I','image','multi_person_face',false),
    ('AUDIO_SINGLE','AUDIO_TTS','audio','',true),
    ('AUDIO_MULTI','AUDIO_MULTI_PERSON','audio','multi_person_audio',false),
    ('VIDEO_SINGLE','FUSION_TALKING_VIDEO','video','',true),
    ('VIDEO_MULTI_GROUP','FUSION_MULTI_PERSON','video','',true)
),
pricing as (
  select
    coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code','') variant_code,
    count(*) filter (where r.status='committed') committed_count,
    max(r.created_at) filter (where r.status='committed') last_commit
  from public.pricing_credit_reservations r
  where r.created_at>=now()-interval '45 days'
  group by 1
),
media as (
  select
    a.kind,
    coalesce(a.meta_json->>'asset_class',sj.meta_json->>'asset_class',sj.payload_json->>'asset_class','') asset_class,
    count(*) count_success,
    max(sj.created_at) last_success
  from public.studio_jobs sj
  join public.artifacts a on a.job_id=sj.id
  where sj.status='succeeded'
    and sj.created_at>=now()-interval '45 days'
    and coalesce(a.bytes,0)>0
  group by 1,2
),
matrix as (
  select
    w.category,w.variant_code,
    coalesce(p.committed_count,0) pricing_commits,
    p.last_commit,
    coalesce(sum(m.count_success) filter (
      where m.kind=w.kind
        and (
          (w.asset_class<>'' and m.asset_class=w.asset_class)
          or
          (w.allow_generic and (m.asset_class='' or m.asset_class is null
            or m.asset_class not in ('group_photo','group_audio','group_video','multi_person_face','multi_person_audio','multi_person_video')))
          or
          (w.category='VIDEO_MULTI_GROUP' and m.asset_class in ('group_video','multi_person_video'))
        )
    ),0) successful_artifacts,
    max(m.last_success) filter (
      where m.kind=w.kind
        and (
          (w.asset_class<>'' and m.asset_class=w.asset_class)
          or
          (w.allow_generic and (m.asset_class='' or m.asset_class is null
            or m.asset_class not in ('group_photo','group_audio','group_video','multi_person_face','multi_person_audio','multi_person_video')))
          or
          (w.category='VIDEO_MULTI_GROUP' and m.asset_class in ('group_video','multi_person_video'))
        )
    ) last_success
  from wanted w
  left join pricing p on p.variant_code=w.variant_code
  left join media m on true
  group by w.category,w.variant_code,p.committed_count,p.last_commit
)
select
  category,
  variant_code,
  pricing_commits,
  last_commit,
  successful_artifacts,
  last_success,
  case when pricing_commits>0 and successful_artifacts>0 then 'PASS' else 'NEEDS_TARGETED_PROOF' end launch_gate
from matrix
order by category;
"

MISSING="$("${PSQL[@]}" -AtF ',' -c "
with wanted(category,variant_code,kind,asset_class,allow_generic) as (
  values
    ('FACE_SINGLE_T2I','FACE_T2I','image','',true),
    ('FACE_SINGLE_I2I','FACE_I2I','image','',true),
    ('FACE_MULTI_T2I','FACE_MULTI_PERSON','image','multi_person_face',false),
    ('FACE_MULTI_I2I','FACE_MULTI_PERSON_I2I','image','multi_person_face',false),
    ('AUDIO_SINGLE','AUDIO_TTS','audio','',true),
    ('AUDIO_MULTI','AUDIO_MULTI_PERSON','audio','multi_person_audio',false),
    ('VIDEO_SINGLE','FUSION_TALKING_VIDEO','video','',true),
    ('VIDEO_MULTI_GROUP','FUSION_MULTI_PERSON','video','',true)
),
pricing as (
  select coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code','') variant_code,
         count(*) filter(where r.status='committed') n
  from public.pricing_credit_reservations r
  where r.created_at>=now()-interval '45 days'
  group by 1
),
media as (
  select a.kind,
         coalesce(a.meta_json->>'asset_class',sj.meta_json->>'asset_class',sj.payload_json->>'asset_class','') asset_class,
         count(*) n
  from public.studio_jobs sj
  join public.artifacts a on a.job_id=sj.id
  where sj.status='succeeded' and sj.created_at>=now()-interval '45 days' and coalesce(a.bytes,0)>0
  group by 1,2
),
matrix as (
 select w.category,
        coalesce(p.n,0) pricing_n,
        coalesce(sum(m.n) filter(where m.kind=w.kind and (
          (w.asset_class<>'' and m.asset_class=w.asset_class)
          or (w.allow_generic and (m.asset_class='' or m.asset_class is null
             or m.asset_class not in ('group_photo','group_audio','group_video','multi_person_face','multi_person_audio','multi_person_video')))
          or (w.category='VIDEO_MULTI_GROUP' and m.asset_class in ('group_video','multi_person_video'))
        )),0) media_n
 from wanted w
 left join pricing p on p.variant_code=w.variant_code
 left join media m on true
 group by w.category,p.n
)
select category from matrix where pricing_n=0 or media_n=0 order by category;
")"

echo
echo "===== 7. ARTIFACT TAXONOMY SAFETY ====="
TAX_BAD="$("${PSQL[@]}" -Atq -c "
select count(*)
from public.artifacts a
join public.studio_jobs sj on sj.id=a.job_id
where sj.created_at>=now()-interval '45 days'
  and coalesce(a.meta_json->>'asset_class',sj.meta_json->>'asset_class',sj.payload_json->>'asset_class','')
      in ('group_photo','multi_person_face')
  and coalesce(a.meta_json->>'reuse_target',sj.meta_json->>'reuse_target',sj.payload_json->>'reuse_target','')
      ilike '%face_studio%';
")"
[[ "$TAX_BAD" == "0" ]] || { echo "ARTIFACT_REUSE_GUARD=FAIL rows=$TAX_BAD"; exit 6; }
echo "ARTIFACT_REUSE_GUARD=PASS"

echo
echo "============================================================"
echo " LAUNCH MATRIX FINAL"
echo "============================================================"
if [[ -z "$MISSING" ]]; then
  echo "TARGETED_GENERATION_REQUIRED=NONE"
  echo "FACE_AUDIO_VIDEO_GENERATION_PRICING_MATRIX=PASS"
else
  echo "TARGETED_GENERATION_REQUIRED=$MISSING"
  echo "FACE_AUDIO_VIDEO_GENERATION_PRICING_MATRIX=NEEDS_TARGETED_PROOF"
fi
echo "production=UNTOUCHED"
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "============================================================"
