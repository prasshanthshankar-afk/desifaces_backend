create or replace view public.v_dashboard_asset_library as
WITH audio_latest AS (
    SELECT
        a.job_id,
        a.id AS artifact_id,
        a.url,
        a.content_type,
        a.bytes,
        a.meta_json,
        a.created_at,
        row_number() OVER (
            PARTITION BY a.job_id
            ORDER BY a.created_at DESC, a.id DESC
        ) AS rn
    FROM artifacts a
    JOIN studio_jobs sj ON sj.id = a.job_id
    WHERE sj.studio_type = 'audio'
      AND a.kind = 'audio'
),
video_latest AS (
    SELECT
        a.job_id,
        a.id AS artifact_id,
        a.url,
        a.content_type,
        a.bytes,
        a.meta_json,
        a.created_at,
        row_number() OVER (
            PARTITION BY a.job_id
            ORDER BY a.created_at DESC, a.id DESC
        ) AS rn
    FROM artifacts a
    JOIN studio_jobs sj ON sj.id = a.job_id
    WHERE sj.studio_type = 'fusion'
      AND a.kind = 'video'
)
SELECT
    'face:'::text || fjo.id::text AS library_id,
    fp.user_id,
    'face'::text AS studio,
    'image'::text AS asset_type,
    COALESCE(
        NULLIF(
            CASE
                WHEN lower(COALESCE(fp.display_name, '')) ~ '^face( variant [0-9]+)?$'
                    THEN ''
                ELSE fp.display_name
            END,
            ''
        ),
        NULLIF(
            TRIM(BOTH ' ' FROM concat_ws(
                ' • ',
                NULLIF(
                    replace(
                        initcap(
                            replace(
                                replace(COALESCE(sj.payload_json ->> 'context_code', ''), '_', ' '),
                                '-',
                                ' '
                            )
                        ),
                        '  ',
                        ' '
                    ),
                    ''
                ),
                CASE
                    WHEN fjo.variant_number IS NOT NULL
                        THEN 'Variant ' || fjo.variant_number::text
                    ELSE NULL
                END
            )),
            ''
        ),
        NULLIF(
            TRIM(BOTH ' ' FROM concat_ws(
                ' • ',
                NULLIF(
                    replace(
                        initcap(
                            replace(
                                replace(COALESCE(sj.payload_json ->> 'use_case_code', ''), '_', ' '),
                                '-',
                                ' '
                            )
                        ),
                        '  ',
                        ' '
                    ),
                    ''
                ),
                CASE
                    WHEN fjo.variant_number IS NOT NULL
                        THEN 'Variant ' || fjo.variant_number::text
                    ELSE NULL
                END
            )),
            ''
        ),
        CASE
            WHEN fjo.variant_number IS NOT NULL
                THEN 'Face • Variant ' || fjo.variant_number::text
            ELSE 'Face'
        END
    ) AS title,
    'ready'::text AS status,
    COALESCE(fjo.created_at, fp.created_at, sj.created_at) AS created_at,
    ma.storage_ref AS thumbnail_url,
    ma.storage_ref AS preview_url,
    ma.storage_ref AS download_url,
    COALESCE(fjo.output_asset_id, ma.id) AS artifact_id,
    ma.id AS media_asset_id,
    sj.id AS source_job_id,
    jsonb_strip_nulls(
        jsonb_build_object(
            'face_artifact_id', COALESCE(fjo.output_asset_id, ma.id)::text,
            'face_profile_id', fp.id::text,
            'media_asset_id', ma.id::text,
            'image_url', ma.storage_ref,
            'variant_number', fjo.variant_number,
            'gender', COALESCE(fp.attributes_json ->> 'gender', sj.payload_json ->> 'gender'),
            'aspect_ratio', COALESCE(fjo.technical_specs ->> 'aspect_ratio', sj.payload_json ->> 'aspect_ratio'),
            'source_image_asset_id',
                CASE
                    WHEN fjo.source_asset_id IS NOT NULL
                        THEN fjo.source_asset_id::text
                    ELSE NULL::text
                END,
            'job_id', sj.id::text
        )
    ) AS reuse_payload_json,
    jsonb_strip_nulls(
        jsonb_build_object(
            'face_job_output_id', fjo.id::text,
            'face_profile_id', fp.id::text,
            'primary_image_asset_id',
                CASE
                    WHEN fp.primary_image_asset_id IS NOT NULL
                        THEN fp.primary_image_asset_id::text
                    ELSE NULL::text
                END,
            'output_asset_id',
                CASE
                    WHEN fjo.output_asset_id IS NOT NULL
                        THEN fjo.output_asset_id::text
                    ELSE NULL::text
                END,
            'source_asset_id',
                CASE
                    WHEN fjo.source_asset_id IS NOT NULL
                        THEN fjo.source_asset_id::text
                    ELSE NULL::text
                END,
            'job_id', sj.id::text,
            'job_status', sj.status,
            'prompt_used', fjo.prompt_used,
            'negative_prompt', fjo.negative_prompt,
            'technical_specs', COALESCE(fjo.technical_specs, '{}'::jsonb),
            'creative_variations', COALESCE(fjo.creative_variations, '{}'::jsonb),
            'identity_score', fjo.identity_score,
            'identity_verified', fjo.identity_verified,
            'profile_attributes', COALESCE(fp.attributes_json, '{}'::jsonb),
            'profile_meta', COALESCE(fp.meta_json, '{}'::jsonb),
            'job_payload', COALESCE(sj.payload_json, '{}'::jsonb),
            'job_meta', COALESCE(sj.meta_json, '{}'::jsonb),
            'asset_kind', ma.kind,
            'asset_content_type', ma.content_type
        )
    ) AS metadata_json
FROM face_job_outputs fjo
JOIN face_profiles fp ON fp.id = fjo.face_profile_id
JOIN studio_jobs sj ON sj.id = fjo.job_id
LEFT JOIN media_assets ma ON ma.id = fjo.output_asset_id
WHERE sj.studio_type = 'face'
  AND sj.status = 'succeeded'
  AND fp.status = 'active'
  AND ma.id IS NOT NULL

UNION ALL

SELECT
    'audio:'::text || sj.id::text AS library_id,
    sj.user_id,
    'audio'::text AS studio,
    'audio'::text AS asset_type,
    COALESCE(
        NULLIF(sj.payload_json ->> 'context', ''),
        NULLIF(sj.payload_json ->> 'voice', ''),
        'Audio'
    ) AS title,
    'ready'::text AS status,
    COALESCE(al.created_at, sj.created_at) AS created_at,
    NULL::text AS thumbnail_url,
    al.url AS preview_url,
    al.url AS download_url,
    al.artifact_id,
    NULL::uuid AS media_asset_id,
    sj.id AS source_job_id,
    jsonb_strip_nulls(
        jsonb_build_object(
            'audio_artifact_id', al.artifact_id::text,
            'audio_url', al.url,
            'locale', COALESCE(sj.payload_json ->> 'target_locale', sj.payload_json ->> 'locale'),
            'voice', sj.payload_json ->> 'voice',
            'duration_sec',
                CASE
                    WHEN COALESCE(al.meta_json ->> 'duration_sec', '') ~ '^[0-9]+(\.[0-9]+)?$'
                        THEN (al.meta_json ->> 'duration_sec')::numeric
                    WHEN COALESCE(al.meta_json ->> 'duration_ms', '') ~ '^[0-9]+(\.[0-9]+)?$'
                        THEN round(((al.meta_json ->> 'duration_ms')::numeric) / 1000.0, 3)
                    ELSE NULL::numeric
                END,
            'job_id', sj.id::text
        )
    ) AS reuse_payload_json,
    jsonb_strip_nulls(
        jsonb_build_object(
            'job_id', sj.id::text,
            'job_status', sj.status,
            'artifact_id', al.artifact_id::text,
            'artifact_content_type', al.content_type,
            'artifact_bytes', al.bytes,
            'artifact_meta', COALESCE(al.meta_json, '{}'::jsonb),
            'payload', COALESCE(sj.payload_json, '{}'::jsonb),
            'meta', COALESCE(sj.meta_json, '{}'::jsonb)
        )
    ) AS metadata_json
FROM studio_jobs sj
JOIN audio_latest al
  ON al.job_id = sj.id
 AND al.rn = 1
WHERE sj.studio_type = 'audio'
  AND sj.status = 'succeeded'
  AND al.url IS NOT NULL

UNION ALL

SELECT
    'video:'::text || fjo.id::text AS library_id,
    COALESCE(dp.user_id, sj.user_id) AS user_id,
    'video'::text AS studio,
    'video'::text AS asset_type,
    'Fusion video'::text AS title,
    CASE
        WHEN dp.status = 'ready' THEN 'ready'
        WHEN sj.status = 'succeeded' THEN 'ready'
        ELSE dp.status
    END AS status,
    COALESCE(fjo.created_at, dp.created_at, sj.created_at) AS created_at,
    NULL::text AS thumbnail_url,
    COALESCE(vl.url, ma.storage_ref, dp.share_url) AS preview_url,
    COALESCE(vl.url, ma.storage_ref, dp.share_url) AS download_url,
    vl.artifact_id,
    ma.id AS media_asset_id,
    sj.id AS source_job_id,
    jsonb_strip_nulls(
        jsonb_build_object(
            'video_artifact_id',
                CASE
                    WHEN vl.artifact_id IS NOT NULL
                        THEN vl.artifact_id::text
                    ELSE NULL::text
                END,
            'video_url', COALESCE(vl.url, ma.storage_ref, dp.share_url),
            'digital_performance_id', dp.id::text,
            'video_asset_id',
                CASE
                    WHEN ma.id IS NOT NULL
                        THEN ma.id::text
                    ELSE NULL::text
                END,
            'face_profile_id',
                CASE
                    WHEN dp.face_profile_id IS NOT NULL
                        THEN dp.face_profile_id::text
                    ELSE NULL::text
                END,
            'audio_clip_id',
                CASE
                    WHEN dp.audio_clip_id IS NOT NULL
                        THEN dp.audio_clip_id::text
                    ELSE NULL::text
                END,
            'provider', dp.provider,
            'job_id', sj.id::text
        )
    ) AS reuse_payload_json,
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
            'video_asset_id',
                CASE
                    WHEN dp.video_asset_id IS NOT NULL
                        THEN dp.video_asset_id::text
                    ELSE NULL::text
                END,
            'artifact_id',
                CASE
                    WHEN vl.artifact_id IS NOT NULL
                        THEN vl.artifact_id::text
                    ELSE NULL::text
                END,
            'artifact_content_type', vl.content_type,
            'artifact_bytes', vl.bytes,
            'artifact_meta', COALESCE(vl.meta_json, '{}'::jsonb),
            'performance_meta', COALESCE(dp.meta_json, '{}'::jsonb),
            'job_payload', COALESCE(sj.payload_json, '{}'::jsonb),
            'job_meta', COALESCE(sj.meta_json, '{}'::jsonb)
        )
    ) AS metadata_json
FROM fusion_job_outputs fjo
JOIN digital_performances dp ON dp.id = fjo.digital_performance_id
JOIN studio_jobs sj ON sj.id = fjo.job_id
LEFT JOIN media_assets ma ON ma.id = dp.video_asset_id
LEFT JOIN video_latest vl
  ON vl.job_id = sj.id
 AND vl.rn = 1
WHERE sj.studio_type = 'fusion'
  AND (dp.status = 'ready' OR sj.status = 'succeeded')
  AND COALESCE(vl.url, ma.storage_ref, dp.share_url) IS NOT NULL;
