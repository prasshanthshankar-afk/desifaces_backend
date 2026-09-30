#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

AUDIO_API="${AUDIO_API:-df-svc-audio}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"

DB_URL="$(docker exec "$AUDIO_API" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — SARVAM RECENT REQUEST CAUSE AUDIT"
echo " READ ONLY / NO SYNTHESIS / NO MUTATION"
echo "============================================================"

"${PSQL[@]}" -P pager=off -c "
with x as (
  select sj.id,sj.created_at,sj.status,sj.payload_json,
         (select a.meta_json
          from public.artifacts a
          where a.job_id=sj.id and a.kind='audio'
          order by a.created_at desc limit 1) artifact_meta
  from public.studio_jobs sj
  where sj.studio_type='audio'
    and sj.created_at>=now()-interval '30 days'
    and coalesce(sj.payload_json->>'target_locale','') in
      ('en-IN','hi-IN','bn-IN','ta-IN','te-IN','kn-IN','ml-IN','mr-IN','gu-IN','pa-IN','or-IN')
)
select
  created_at,id job_id,status,
  payload_json->>'target_locale' locale,
  coalesce(nullif(payload_json->>'style',''),'NO_STYLE') style,
  coalesce(nullif(payload_json->>'emotion',''),'NO_EMOTION') emotion,
  coalesce(nullif(payload_json->>'voice',''),nullif(payload_json->>'voice_id',''),'AUTO') requested_voice,
  coalesce(artifact_meta->>'provider_code',payload_json->'tts_meta'->>'provider_code','unknown') actual_provider,
  coalesce(artifact_meta->>'model_code',payload_json->'tts_meta'->>'model_code','unknown') actual_model
from x
order by created_at desc;
"

echo
echo "===== REQUEST SHAPE SUMMARY ====="
"${PSQL[@]}" -P pager=off -c "
with x as (
  select sj.payload_json,
         (select a.meta_json from public.artifacts a
          where a.job_id=sj.id and a.kind='audio'
          order by a.created_at desc limit 1) artifact_meta
  from public.studio_jobs sj
  where sj.studio_type='audio'
    and sj.created_at>=now()-interval '30 days'
    and coalesce(sj.payload_json->>'target_locale','') in
      ('en-IN','hi-IN','bn-IN','ta-IN','te-IN','kn-IN','ml-IN','mr-IN','gu-IN','pa-IN','or-IN')
)
select
  payload_json->>'target_locale' locale,
  case when nullif(payload_json->>'style','') is null then 'NO_STYLE' else payload_json->>'style' end style,
  case when coalesce(nullif(payload_json->>'voice',''),nullif(payload_json->>'voice_id','')) is null
       then 'AUTO_VOICE' else 'EXPLICIT_VOICE' end voice_mode,
  coalesce(artifact_meta->>'provider_code',payload_json->'tts_meta'->>'provider_code','unknown') actual_provider,
  count(*) jobs
from x
group by 1,2,3,4
order by 1,2,3,4;
"

echo
echo "===== EXPLICIT VOICE OWNERSHIP ====="
"${PSQL[@]}" -P pager=off -c "
with r as (
  select
    sj.payload_json->>'target_locale' locale,
    coalesce(nullif(sj.payload_json->>'voice',''),nullif(sj.payload_json->>'voice_id','')) requested_voice
  from public.studio_jobs sj
  where sj.studio_type='audio'
    and sj.created_at>=now()-interval '30 days'
    and coalesce(sj.payload_json->>'target_locale','') in
      ('en-IN','hi-IN','bn-IN','ta-IN','te-IN','kn-IN','ml-IN','mr-IN','gu-IN','pa-IN','or-IN')
)
select r.locale,r.requested_voice,
       coalesce(string_agg(distinct v.provider,',' order by v.provider),'NO_MATCH') provider_owner,
       count(*) jobs
from r
left join public.tts_voices v
  on r.requested_voice is not null and lower(v.voice_name)=lower(r.requested_voice)
where r.requested_voice is not null
group by 1,2
order by jobs desc,1,2;
"

echo
echo "generation_mutation=NONE"
echo "db_mutation=NONE"
echo "runtime_mutation=NONE"
echo "production=UNTOUCHED"
echo "SARVAM_RECENT_REQUEST_CAUSE_AUDIT=COMPLETE"
