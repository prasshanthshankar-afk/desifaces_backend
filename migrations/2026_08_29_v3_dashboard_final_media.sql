-- desifaces V3 Dashboard / Saved Work canonical Fusion-final visibility
--
-- Product invariant:
--   * workflow child/scene/segment/dialogue-turn renders remain internal
--   * exactly the canonical media_assets row referenced by
--     v3_studio_workflows.final_media_id is user-visible
--   * media_assets.role is NOT a finality signal (canonical finals may still
--     carry legacy role=preview)
--
-- This migration extends the existing shared dashboard read model instead of
-- duplicating Story/Fusion visibility logic in individual API/UI consumers.
-- It is intentionally idempotent and preserves the existing view definition.

DO $migration$
DECLARE
    base_definition text;
    view_columns text[];
    expected_columns constant text[] := ARRAY[
        'library_id',
        'user_id',
        'studio',
        'asset_type',
        'title',
        'status',
        'created_at',
        'thumbnail_url',
        'preview_url',
        'download_url',
        'artifact_id',
        'media_asset_id',
        'source_job_id',
        'reuse_payload_json',
        'metadata_json'
    ];
BEGIN
    IF to_regclass('public.v_dashboard_asset_library') IS NULL THEN
        RAISE EXCEPTION 'required view public.v_dashboard_asset_library does not exist';
    END IF;
    IF to_regclass('public.v3_studio_workflows') IS NULL THEN
        RAISE EXCEPTION 'required table public.v3_studio_workflows does not exist';
    END IF;
    IF to_regclass('public.media_assets') IS NULL THEN
        RAISE EXCEPTION 'required table public.media_assets does not exist';
    END IF;

    SELECT array_agg(column_name::text ORDER BY ordinal_position)
      INTO view_columns
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'v_dashboard_asset_library';

    IF view_columns IS DISTINCT FROM expected_columns THEN
        RAISE EXCEPTION
            'v_dashboard_asset_library contract changed; expected %, found %',
            expected_columns,
            view_columns;
    END IF;

    base_definition := pg_get_viewdef('public.v_dashboard_asset_library'::regclass, true);

    -- Re-running the migration must never stack another UNION around the view.
    IF base_definition ILIKE '%v3_studio_workflows%'
       AND base_definition ILIKE '%final_media_id%'
       AND base_definition ILIKE '%canonical_final%' THEN
        RAISE NOTICE 'v_dashboard_asset_library already contains canonical V3 final-media projection';
        RETURN;
    END IF;

    EXECUTE format($view$
        CREATE OR REPLACE VIEW public.v_dashboard_asset_library AS
        WITH existing_library AS (
            %s
        ),
        workflow_source AS (
            SELECT
                w.id AS workflow_id,
                w.user_id,
                w.final_media_id,
                to_jsonb(w) AS workflow_json
            FROM public.v3_studio_workflows w
            WHERE w.final_media_id IS NOT NULL
              AND lower(coalesce(
                    to_jsonb(w)->>'status',
                    to_jsonb(w)->>'state',
                    to_jsonb(w)->>'workflow_status',
                    ''
                  )) ~ '^(ready|succeeded|success|complete|completed)$'
        ),
        canonical_source AS (
            SELECT
                ws.workflow_id,
                ws.user_id,
                ws.final_media_id,
                ws.workflow_json,
                m.id AS media_id,
                to_jsonb(m) AS media_json
            FROM workflow_source ws
            JOIN public.media_assets m
              ON m.id = ws.final_media_id
            WHERE lower(coalesce(
                    to_jsonb(m)->>'mime_type',
                    to_jsonb(m)->>'content_type',
                    'video/mp4'
                  )) LIKE 'video/%'
              AND lower(coalesce(to_jsonb(m)->>'is_active', 'true')) NOT IN ('false', '0', 'no')
              AND lower(coalesce(to_jsonb(m)->>'is_deleted', 'false')) NOT IN ('true', '1', 'yes')
              AND nullif(to_jsonb(m)->>'deleted_at', '') IS NULL
        ),
        canonical_scored AS (
            SELECT
                cs.*,
                coalesce(
                    nullif(cs.media_json->>'url', ''),
                    nullif(cs.media_json->>'media_url', ''),
                    nullif(cs.media_json->>'video_url', ''),
                    nullif(cs.media_json->>'preview_url', ''),
                    nullif(cs.media_json->>'download_url', ''),
                    nullif(cs.media_json->>'storage_url', ''),
                    nullif(cs.media_json->>'blob_url', ''),
                    nullif(cs.media_json->>'public_url', ''),
                    nullif(substring(cs.media_json::text from '(https?://[^" ]+\\.mp4[^" ]*)'), '')
                ) AS video_url,
                coalesce(
                    nullif(cs.media_json->>'thumbnail_url', ''),
                    nullif(cs.media_json->>'poster_url', ''),
                    nullif(cs.media_json->>'cover_url', ''),
                    nullif(cs.workflow_json->>'thumbnail_url', ''),
                    nullif(cs.workflow_json->>'poster_url', ''),
                    nullif(cs.workflow_json->>'cover_url', ''),
                    nullif(substring(cs.workflow_json::text from '(https?://[^" ]+\\.(jpg|jpeg|png|webp)[^" ]*)'), '')
                ) AS thumbnail_url
            FROM canonical_source cs
        ),
        canonical_rows AS (
            SELECT
                ('video:' || media_id::text)::text AS library_id,
                user_id::uuid AS user_id,
                'video'::text AS studio,
                'video'::text AS asset_type,
                coalesce(
                    nullif(workflow_json->>'title', ''),
                    nullif(workflow_json->>'story_title', ''),
                    nullif(workflow_json #>> '{metadata,title}', ''),
                    nullif(workflow_json #>> '{metadata,story_title}', ''),
                    'Final Video'
                )::text AS title,
                'completed'::text AS status,
                coalesce(
                    CASE WHEN coalesce(media_json->>'created_at', '') ~ '^\\d{4}-\\d{2}-\\d{2}'
                         THEN (media_json->>'created_at')::timestamptz END,
                    CASE WHEN coalesce(media_json->>'updated_at', '') ~ '^\\d{4}-\\d{2}-\\d{2}'
                         THEN (media_json->>'updated_at')::timestamptz END,
                    CASE WHEN coalesce(workflow_json->>'completed_at', '') ~ '^\\d{4}-\\d{2}-\\d{2}'
                         THEN (workflow_json->>'completed_at')::timestamptz END,
                    CASE WHEN coalesce(workflow_json->>'updated_at', '') ~ '^\\d{4}-\\d{2}-\\d{2}'
                         THEN (workflow_json->>'updated_at')::timestamptz END,
                    CASE WHEN coalesce(workflow_json->>'created_at', '') ~ '^\\d{4}-\\d{2}-\\d{2}'
                         THEN (workflow_json->>'created_at')::timestamptz END,
                    now()
                ) AS created_at,
                thumbnail_url::text AS thumbnail_url,
                video_url::text AS preview_url,
                video_url::text AS download_url,
                null::uuid AS artifact_id,
                media_id::uuid AS media_asset_id,
                workflow_id::uuid AS source_job_id,
                jsonb_strip_nulls(jsonb_build_object(
                    'media_asset_id', media_id,
                    'video_media_asset_id', media_id,
                    'video_url', video_url,
                    'thumbnail_url', thumbnail_url,
                    'poster_url', thumbnail_url,
                    'source_job_id', workflow_id,
                    'workflow_id', workflow_id,
                    'final_media_id', media_id,
                    'output_role', 'final',
                    'render_kind', 'final',
                    'canonical_final', true
                )) AS reuse_payload_json,
                jsonb_strip_nulls(jsonb_build_object(
                    'provider', coalesce(nullif(media_json->>'provider', ''), 'svc-fusion-extension'),
                    'source_table', 'v3_studio_workflows+media_assets',
                    'workflow_id', workflow_id,
                    'final_media_id', media_id,
                    'media_asset_id', media_id,
                    'render_kind', 'final',
                    'output_role', 'final',
                    'canonical_final', true,
                    'share_url', video_url,
                    'thumbnail_url', thumbnail_url,
                    'poster_url', thumbnail_url,
                    'artifact_content_type', coalesce(media_json->>'mime_type', media_json->>'content_type', 'video/mp4'),
                    'workflow', workflow_json,
                    'media_asset', media_json,
                    'performance_meta', jsonb_build_object(
                        'video_url', video_url,
                        'thumbnail_url', thumbnail_url,
                        'poster_url', thumbnail_url,
                        'status', 'completed'
                    )
                )) AS metadata_json
            FROM canonical_scored
            WHERE video_url IS NOT NULL
        )
        SELECT *
        FROM existing_library

        UNION ALL

        SELECT cr.*
        FROM canonical_rows cr
        WHERE NOT EXISTS (
            SELECT 1
            FROM existing_library e
            WHERE e.user_id = cr.user_id
              AND lower(coalesce(e.studio, '')) = 'video'
              AND (
                    e.media_asset_id = cr.media_asset_id
                 OR e.source_job_id = cr.source_job_id
                 OR nullif(coalesce(e.preview_url, e.download_url), '') = cr.preview_url
              )
        )
    $view$, base_definition);

    COMMENT ON VIEW public.v_dashboard_asset_library IS
        'Shared desifaces Dashboard/Saved Work asset read model. V3 Fusion/Story videos are user-visible only when they are the canonical media_assets row referenced by a completed v3_studio_workflows.final_media_id; child/scene/segment renders remain internal.';
END
$migration$;
