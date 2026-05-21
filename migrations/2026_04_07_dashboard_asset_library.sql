begin;

drop view if exists public.v_dashboard_asset_library;

create view public.v_dashboard_asset_library as
with audio_latest as (
    select
        a.job_id,
        a.id as artifact_id,
        a.url,
        a.content_type,
        a.bytes,
        a.meta_json,
        a.created_at,
        row_number() over (
            partition by a.job_id
            order by a.created_at desc, a.id desc
        ) as rn
    from public.artifacts a
    join public.studio_jobs sj
      on sj.id = a.job_id
    where sj.studio_type = 'audio'
      and a.kind = 'audio'
),
video_latest as (
    select
        a.job_id,
        a.id as artifact_id,
        a.url,
        a.content_type,
        a.bytes,
        a.meta_json,
        a.created_at,
        row_number() over (
            partition by a.job_id
            order by a.created_at desc, a.id desc
        ) as rn
    from public.artifacts a
    join public.studio_jobs sj
      on sj.id = a.job_id
    where sj.studio_type = 'fusion'
      and a.kind = 'video'
)

select
    ('face:' || fjo.id::text) as library_id,
    fp.user_id,
    'face'::text as studio,
    'image'::text as asset_type,
    coalesce(
        nullif(
            case
                when lower(coalesce(fp.display_name, '')) ~ '^face( variant [0-9]+)?$' then ''
                else fp.display_name
            end,
            ''
        ),
        nullif(
            trim(
                both ' ' from concat_ws(
                    ' • ',
                    nullif(
                        replace(
                            initcap(replace(replace(coalesce(sj.payload_json->>'context_code', ''), '_', ' '), '-', ' ')),
                            '  ',
                            ' '
                        ),
                        ''
                    ),
                    case
                        when fjo.variant_number is not null then 'Variant ' || fjo.variant_number::text
                        else null
                    end
                )
            ),
            ''
        ),
        nullif(
            trim(
                both ' ' from concat_ws(
                    ' • ',
                    nullif(
                        replace(
                            initcap(replace(replace(coalesce(sj.payload_json->>'use_case_code', ''), '_', ' '), '-', ' ')),
                            '  ',
                            ' '
                        ),
                        ''
                    ),
                    case
                        when fjo.variant_number is not null then 'Variant ' || fjo.variant_number::text
                        else null
                    end
                )
            ),
            ''
        ),
        case
            when fjo.variant_number is not null then 'Face • Variant ' || fjo.variant_number::text
            else 'Face'
        end
    ) as title,
    'ready'::text as status,
    coalesce(fjo.created_at, fp.created_at, sj.created_at) as created_at,
    ma.storage_ref as thumbnail_url,
    ma.storage_ref as preview_url,
    ma.storage_ref as download_url,
    null::uuid as artifact_id,
    ma.id as media_asset_id,
    sj.id as source_job_id,
    jsonb_strip_nulls(
        jsonb_build_object(
            'face_profile_id', fp.id::text,
            'media_asset_id', ma.id::text,
            'image_url', ma.storage_ref,
            'variant_number', fjo.variant_number,
            'gender', coalesce(fp.attributes_json->>'gender', sj.payload_json->>'gender'),
            'aspect_ratio', coalesce(
                fjo.technical_specs->>'aspect_ratio',
                sj.payload_json->>'aspect_ratio'
            ),
            'source_image_asset_id', case when fjo.source_asset_id is not null then fjo.source_asset_id::text end,
            'job_id', sj.id::text
        )
    ) as reuse_payload_json,
    jsonb_strip_nulls(
        jsonb_build_object(
            'face_job_output_id', fjo.id::text,
            'face_profile_id', fp.id::text,
            'primary_image_asset_id', case when fp.primary_image_asset_id is not null then fp.primary_image_asset_id::text end,
            'output_asset_id', case when fjo.output_asset_id is not null then fjo.output_asset_id::text end,
            'source_asset_id', case when fjo.source_asset_id is not null then fjo.source_asset_id::text end,
            'job_id', sj.id::text,
            'job_status', sj.status,
            'prompt_used', fjo.prompt_used,
            'negative_prompt', fjo.negative_prompt,
            'technical_specs', coalesce(fjo.technical_specs, '{}'::jsonb),
            'creative_variations', coalesce(fjo.creative_variations, '{}'::jsonb),
            'identity_score', fjo.identity_score,
            'identity_verified', fjo.identity_verified,
            'profile_attributes', coalesce(fp.attributes_json, '{}'::jsonb),
            'profile_meta', coalesce(fp.meta_json, '{}'::jsonb),
            'job_payload', coalesce(sj.payload_json, '{}'::jsonb),
            'job_meta', coalesce(sj.meta_json, '{}'::jsonb),
            'asset_kind', ma.kind,
            'asset_content_type', ma.content_type
        )
    ) as metadata_json
from public.face_job_outputs fjo
join public.face_profiles fp
  on fp.id = fjo.face_profile_id
join public.studio_jobs sj
  on sj.id = fjo.job_id
left join public.media_assets ma
  on ma.id = fjo.output_asset_id
where sj.studio_type = 'face'
  and sj.status = 'succeeded'
  and fp.status = 'active'
  and ma.id is not null

union all

select
    ('audio:' || sj.id::text) as library_id,
    sj.user_id,
    'audio'::text as studio,
    'audio'::text as asset_type,
    coalesce(
        nullif(sj.payload_json->>'context', ''),
        nullif(sj.payload_json->>'voice', ''),
        'Audio'
    ) as title,
    'ready'::text as status,
    coalesce(al.created_at, sj.created_at) as created_at,
    null::text as thumbnail_url,
    al.url as preview_url,
    al.url as download_url,
    al.artifact_id as artifact_id,
    null::uuid as media_asset_id,
    sj.id as source_job_id,
    jsonb_strip_nulls(
        jsonb_build_object(
            'audio_artifact_id', al.artifact_id::text,
            'audio_url', al.url,
            'locale', coalesce(
                sj.payload_json->>'target_locale',
                sj.payload_json->>'locale'
            ),
            'voice', sj.payload_json->>'voice',
            'duration_sec',
                case
                    when coalesce(al.meta_json->>'duration_sec', '') ~ '^[0-9]+(\.[0-9]+)?$'
                        then (al.meta_json->>'duration_sec')::numeric
                    when coalesce(al.meta_json->>'duration_ms', '') ~ '^[0-9]+(\.[0-9]+)?$'
                        then round(((al.meta_json->>'duration_ms')::numeric / 1000.0), 3)
                    else null
                end,
            'job_id', sj.id::text
        )
    ) as reuse_payload_json,
    jsonb_strip_nulls(
        jsonb_build_object(
            'job_id', sj.id::text,
            'job_status', sj.status,
            'artifact_id', al.artifact_id::text,
            'artifact_content_type', al.content_type,
            'artifact_bytes', al.bytes,
            'artifact_meta', coalesce(al.meta_json, '{}'::jsonb),
            'payload', coalesce(sj.payload_json, '{}'::jsonb),
            'meta', coalesce(sj.meta_json, '{}'::jsonb)
        )
    ) as metadata_json
from public.studio_jobs sj
join audio_latest al
  on al.job_id = sj.id
 and al.rn = 1
where sj.studio_type = 'audio'
  and sj.status = 'succeeded'
  and al.url is not null

union all

select
    ('video:' || fjo.id::text) as library_id,
    coalesce(dp.user_id, sj.user_id) as user_id,
    'video'::text as studio,
    'video'::text as asset_type,
    'Fusion video'::text as title,
    case
        when dp.status = 'ready' then 'ready'
        when sj.status = 'succeeded' then 'ready'
        else dp.status
    end as status,
    coalesce(fjo.created_at, dp.created_at, sj.created_at) as created_at,
    null::text as thumbnail_url,
    coalesce(vl.url, ma.storage_ref, dp.share_url) as preview_url,
    coalesce(vl.url, ma.storage_ref, dp.share_url) as download_url,
    vl.artifact_id as artifact_id,
    ma.id as media_asset_id,
    sj.id as source_job_id,
    jsonb_strip_nulls(
        jsonb_build_object(
            'video_artifact_id', case when vl.artifact_id is not null then vl.artifact_id::text end,
            'video_url', coalesce(vl.url, ma.storage_ref, dp.share_url),
            'digital_performance_id', dp.id::text,
            'video_asset_id', case when ma.id is not null then ma.id::text end,
            'face_profile_id', case when dp.face_profile_id is not null then dp.face_profile_id::text end,
            'audio_clip_id', case when dp.audio_clip_id is not null then dp.audio_clip_id::text end,
            'provider', dp.provider,
            'job_id', sj.id::text
        )
    ) as reuse_payload_json,
    jsonb_strip_nulls(
        jsonb_build_object(
            'fusion_job_output_id', fjo.id::text,
            'digital_performance_id', dp.id::text,
            'job_id', sj.id::text,
            'job_status', sj.status,
            'performance_status', dp.status,
            'provider', dp.provider,
            'provider_job_id', dp.provider_job_id,
            'share_url', dp.share_url,
            'video_asset_id', case when dp.video_asset_id is not null then dp.video_asset_id::text end,
            'artifact_id', case when vl.artifact_id is not null then vl.artifact_id::text end,
            'artifact_content_type', vl.content_type,
            'artifact_bytes', vl.bytes,
            'artifact_meta', coalesce(vl.meta_json, '{}'::jsonb),
            'performance_meta', coalesce(dp.meta_json, '{}'::jsonb),
            'job_payload', coalesce(sj.payload_json, '{}'::jsonb),
            'job_meta', coalesce(sj.meta_json, '{}'::jsonb)
        )
    ) as metadata_json
from public.fusion_job_outputs fjo
join public.digital_performances dp
  on dp.id = fjo.digital_performance_id
join public.studio_jobs sj
  on sj.id = fjo.job_id
left join public.media_assets ma
  on ma.id = dp.video_asset_id
left join video_latest vl
  on vl.job_id = sj.id
 and vl.rn = 1
where sj.studio_type = 'fusion'
  and (
        dp.status = 'ready'
        or sj.status = 'succeeded'
      )
  and coalesce(vl.url, ma.storage_ref, dp.share_url) is not null
;

commit;

-- smoke test:
-- select studio, asset_type, count(*) from public.v_dashboard_asset_library group by 1,2 order by 1,2;
-- select library_id, studio, title, created_at from public.v_dashboard_asset_library order by created_at desc limit 20;
