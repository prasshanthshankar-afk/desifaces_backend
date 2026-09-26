-- #next3 multi-person execution integrity.
-- No product pricing/schema redesign. These guards make existing canonical
-- workflow/attempt/media relationships fail closed under retries/concurrency.

BEGIN;

-- One Studio stage may have only one execution attempt in a non-terminal state.
-- Service code already serializes dispatch using the stage row; this database
-- guard prevents duplicate active attempts even if two runtimes race.
DO $$
DECLARE
  v_stage uuid;
BEGIN
  SELECT stage_run_id
    INTO v_stage
  FROM public.v3_studio_stage_attempts
  WHERE state IN ('dispatching','queued','running')
  GROUP BY stage_run_id
  HAVING count(*) > 1
  LIMIT 1;

  IF v_stage IS NOT NULL THEN
    RAISE EXCEPTION
      'next3 integrity preflight failed: multiple active attempts for stage %',
      v_stage;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_studio_stage_one_active_attempt
  ON public.v3_studio_stage_attempts(stage_run_id)
  WHERE state IN ('dispatching','queued','running');

-- Every confirmed shared-scene Fusion stage must have a durable relational
-- reference to the authoritative group photo, not only JSON metadata. Fail
-- closed if existing metadata points outside the workflow account/project.
DO $
DECLARE
  v_stage uuid;
BEGIN
  SELECT s.stage_run_id
    INTO v_stage
  FROM public.v3_studio_stage_runs s
  JOIN public.v3_studio_workflows w ON w.workflow_id=s.workflow_id
  LEFT JOIN public.media_assets m
    ON m.id = nullif(s.metadata_json->>'shared_scene_media_id','')::uuid
  WHERE s.stage_type='fusion'
    AND s.scope_type='scene'
    AND s.metadata_json->>'conversation_mode'='shared_scene'
    AND nullif(s.metadata_json->>'shared_scene_media_id','') IS NOT NULL
    AND (
      m.id IS NULL
      OR m.lifecycle_state <> 'active'
      OR (m.account_id IS NOT NULL AND m.account_id <> w.account_id)
      OR (m.project_id IS NOT NULL AND m.project_id <> w.project_id)
    )
  LIMIT 1;

  IF v_stage IS NOT NULL THEN
    RAISE EXCEPTION
      'next3 integrity preflight failed: invalid shared-scene media lineage for stage %',
      v_stage;
  END IF;
END $;

INSERT INTO public.v3_studio_stage_inputs(
  stage_run_id,media_id,input_role,source_stage_run_id
)
SELECT
  s.stage_run_id,
  (s.metadata_json->>'shared_scene_media_id')::uuid,
  'approved_shared_scene_image',
  NULL
FROM public.v3_studio_stage_runs s
JOIN public.v3_studio_workflows w ON w.workflow_id=s.workflow_id
JOIN public.media_assets m
  ON m.id=(s.metadata_json->>'shared_scene_media_id')::uuid
WHERE s.stage_type='fusion'
  AND s.scope_type='scene'
  AND s.metadata_json->>'conversation_mode'='shared_scene'
  AND nullif(s.metadata_json->>'shared_scene_media_id','') IS NOT NULL
  AND m.lifecycle_state='active'
  AND (m.account_id IS NULL OR m.account_id=w.account_id)
  AND (m.project_id IS NULL OR m.project_id=w.project_id)
ON CONFLICT(stage_run_id,media_id,input_role) DO NOTHING;

-- A canonical scene-stitch attempt owns at most one active final media asset.
-- Retry attempts intentionally get different attempt ids and therefore distinct
-- provenance; replays of the same attempt must converge on one asset.
DO $$
DECLARE
  v_attempt text;
BEGIN
  SELECT meta_json->>'v3_studio_attempt_id'
    INTO v_attempt
  FROM public.media_assets
  WHERE lifecycle_state='active'
    AND kind='video'
    AND meta_json->>'source_kind'='v3_scene_stitch'
    AND nullif(meta_json->>'v3_studio_attempt_id','') IS NOT NULL
  GROUP BY meta_json->>'v3_studio_attempt_id'
  HAVING count(*) > 1
  LIMIT 1;

  IF v_attempt IS NOT NULL THEN
    RAISE EXCEPTION
      'next3 integrity preflight failed: duplicate scene media for attempt %',
      v_attempt;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_media_v3_scene_stitch_attempt
  ON public.media_assets ((meta_json->>'v3_studio_attempt_id'))
  WHERE lifecycle_state='active'
    AND kind='video'
    AND meta_json->>'source_kind'='v3_scene_stitch'
    AND nullif(meta_json->>'v3_studio_attempt_id','') IS NOT NULL;

COMMIT;
