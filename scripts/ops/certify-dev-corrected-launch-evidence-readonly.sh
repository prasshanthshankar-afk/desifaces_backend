#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — CORRECTED LAUNCH EVIDENCE"
echo " READ ONLY / NO GENERATION / NO MUTATION"
echo "============================================================"

echo
echo "===== 1. FACE ACTUAL ARTIFACT KINDS + PRICING VARIANTS ====="
"${PSQL[@]}" -P pager=off -c "
select
  coalesce(
    sj.payload_json->'pricing'->>'variant_code',
    sj.meta_json->'pricing'->>'variant_code',
    sj.payload_json->'pricing'->>'sku_code',
    sj.meta_json->'pricing'->>'sku_code',
    'NO_VARIANT'
  ) pricing_variant,
  a.kind,
  count(*) artifacts,
  max(sj.created_at) last_success
from public.studio_jobs sj
join public.artifacts a on a.job_id=sj.id
where sj.status='succeeded'
  and sj.created_at>=now()-interval '45 days'
  and a.kind in ('face_image','face','image')
  and coalesce(a.bytes,0)>0
group by 1,2
order by 1,2;
"

echo
echo "===== 2. CANONICAL DASHBOARD / SAVED-WORK CLASSIFICATION ====="
if "${PSQL[@]}" -Atq -c "select case when to_regclass('public.v_dashboard_asset_library') is not null then 1 else 0 end;" | grep -qx 1; then
  "${PSQL[@]}" -P pager=off -c "
  with x as (
    select to_jsonb(v) j
    from public.v_dashboard_asset_library v
  )
  select
    coalesce(j->>'studio','') studio,
    coalesce(j->>'asset_class',j#>>'{reuse_payload,asset_class}','UNCLASSIFIED') asset_class,
    coalesce(j->>'workflow_kind',j->>'conversation_kind',j#>>'{reuse_payload,workflow_kind}',j#>>'{reuse_payload,conversation_kind}','') workflow_kind,
    count(*) rows,
    max(coalesce(j->>'created_at',j->>'updated_at')) latest
  from x
  group by 1,2,3
  order by 1,2,3;
  "
else
  echo "DASHBOARD_LIBRARY_VIEW=MISSING"
fi

echo
echo "===== 3. FUSION OUTPUT STORAGE EVIDENCE ====="
for table in fusion_job_outputs digital_performances media_assets v3_studio_stage_runs v3_studio_stage_outputs; do
  exists="$("${PSQL[@]}" -Atq -c "select case when to_regclass('public.$table') is not null then 1 else 0 end;")"
  echo "TABLE=$table exists=$exists"
done

if "${PSQL[@]}" -Atq -c "select case when to_regclass('public.fusion_job_outputs') is not null then 1 else 0 end;" | grep -qx 1; then
  "${PSQL[@]}" -P pager=off -c "
  select
    count(*) rows,
    count(*) filter(where lower(coalesce(to_jsonb(f)->>'status','')) in ('succeeded','completed','complete','ready')) terminal_success_rows,
    max(coalesce(to_jsonb(f)->>'created_at',to_jsonb(f)->>'updated_at')) latest
  from public.fusion_job_outputs f;
  "
fi

if "${PSQL[@]}" -Atq -c "select case when to_regclass('public.media_assets') is not null then 1 else 0 end;" | grep -qx 1; then
  "${PSQL[@]}" -P pager=off -c "
  select
    kind,
    coalesce(meta_json->>'asset_class','UNCLASSIFIED') asset_class,
    coalesce(meta_json->>'workflow_kind',meta_json->>'conversation_kind','') workflow_kind,
    count(*) rows,
    max(created_at) latest
  from public.media_assets
  where created_at>=now()-interval '45 days'
    and kind in ('video','image','face','audio')
  group by 1,2,3
  order by 1,2,3;
  "
fi

echo
echo "===== 4. PRICING COMMIT TRUTH ====="
"${PSQL[@]}" -P pager=off -c "
with wanted(code) as (
 values
 ('FACE_T2I'),('FACE_I2I'),('FACE_MULTI_PERSON'),('FACE_MULTI_PERSON_I2I'),
 ('AUDIO_TTS'),('AUDIO_MULTI_PERSON'),
 ('FUSION_TALKING_VIDEO'),('FUSION_MULTI_PERSON')
)
select
 w.code,
 count(r.*) filter(where r.status='committed') committed,
 max(r.created_at) filter(where r.status='committed') last_commit,
 bool_or(
   r.status='committed'
   and coalesce((r.quote_json->'economics'->>'has_costs_complete')::boolean,false)=true
   and r.quote_json->'economics'->>'cogs_money_final' is not null
   and jsonb_array_length(coalesce(r.quote_json->'economics'->'missing_cost_skus','[]'::jsonb))=0
 ) economics_complete
from wanted w
left join public.pricing_credit_reservations r
  on coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code','')=w.code
 and r.created_at>=now()-interval '45 days'
group by w.code
order by w.code;
"

echo
echo "===== 5. ACTUAL SARVAM SUCCESS ====="
"${PSQL[@]}" -P pager=off -c "
select
 count(*) successful_sarvam_artifacts,
 max(sj.created_at) last_sarvam_success
from public.studio_jobs sj
join public.artifacts a on a.job_id=sj.id
where sj.status='succeeded'
  and a.kind='audio'
  and coalesce(a.bytes,0)>0
  and lower(coalesce(a.meta_json->>'provider_code',sj.payload_json->'tts_meta'->>'provider_code',''))='sarvam';
"

echo
echo "===== 6. CURRENT GAPS THAT REQUIRE REAL GENERATION ====="
"${PSQL[@]}" -P pager=off -c "
with p as (
 select coalesce(quote_json->>'variant_code',quote_json->>'sku_code','') v,
        count(*) filter(where status='committed') n
 from public.pricing_credit_reservations
 where created_at>=now()-interval '45 days'
 group by 1
),
sarvam as (
 select count(*) n
 from public.studio_jobs sj join public.artifacts a on a.job_id=sj.id
 where sj.status='succeeded' and a.kind='audio' and coalesce(a.bytes,0)>0
   and lower(coalesce(a.meta_json->>'provider_code',sj.payload_json->'tts_meta'->>'provider_code',''))='sarvam'
)
select gap,reason from (
 values
 ('FACE_MULTI_PERSON_I2I',case when coalesce((select n from p where v='FACE_MULTI_PERSON_I2I'),0)=0 then 'MISSING_COMMITTED_PRICING' else 'EVIDENCE_PRESENT' end),
 ('AUDIO_MULTI_PERSON',case when coalesce((select n from p where v='AUDIO_MULTI_PERSON'),0)=0 then 'MISSING_COMMITTED_PRICING' else 'EVIDENCE_PRESENT' end),
 ('FUSION_MULTI_PERSON',case when coalesce((select n from p where v='FUSION_MULTI_PERSON'),0)=0 then 'MISSING_COMMITTED_PRICING' else 'EVIDENCE_PRESENT' end),
 ('AUDIO_SARVAM_ACTUAL',case when (select n from sarvam)=0 then 'NO_SUCCESSFUL_PROVIDER_CALL' else 'EVIDENCE_PRESENT' end)
) x(gap,reason)
where reason<>'EVIDENCE_PRESENT';
"

echo
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"
echo "CORRECTED_LAUNCH_EVIDENCE=COMPLETE"
