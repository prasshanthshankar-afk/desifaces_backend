-- Harden V3 staged Studio HITL workflow for variant/supersession semantics.
BEGIN;

ALTER TABLE public.v3_studio_stage_outputs
  ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS idx_v3_studio_stage_outputs_active
  ON public.v3_studio_stage_outputs(stage_run_id, created_at, media_id)
  WHERE is_active=true;

-- A scope has exactly the identifiers it needs and no unrelated IDs.
ALTER TABLE public.v3_studio_stage_runs
  DROP CONSTRAINT IF EXISTS ck_v3_studio_stage_scope;
ALTER TABLE public.v3_studio_stage_runs
  ADD CONSTRAINT ck_v3_studio_stage_scope CHECK (
    (scope_type='participant' AND participant_id IS NOT NULL AND scene_id IS NULL AND dialogue_turn_id IS NULL) OR
    (scope_type='dialogue_turn' AND participant_id IS NULL AND scene_id IS NULL AND dialogue_turn_id IS NOT NULL) OR
    (scope_type='scene' AND participant_id IS NULL AND scene_id IS NOT NULL AND dialogue_turn_id IS NULL) OR
    (scope_type='story' AND participant_id IS NULL AND scene_id IS NULL AND dialogue_turn_id IS NULL)
  );

-- Downstream input must be an ACTIVE approved output from the referenced stage.
CREATE OR REPLACE FUNCTION public.df_v3_validate_stage_input_approved()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source_stage_run_id IS NOT NULL THEN
    IF NOT EXISTS (
      SELECT 1
      FROM public.v3_studio_stage_outputs o
      JOIN public.v3_studio_review_items r
        ON r.stage_run_id=o.stage_run_id AND r.media_id=o.media_id
      WHERE o.stage_run_id=NEW.source_stage_run_id
        AND o.media_id=NEW.media_id
        AND o.is_active=true
        AND r.decision='approved'
    ) THEN
      RAISE EXCEPTION 'v3_studio_input_requires_active_approved_upstream_output:stage=% media=% source=%',
        NEW.stage_run_id, NEW.media_id, NEW.source_stage_run_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

-- Stage approval requires at least one active output and every active output to
-- have an approved review. Rejected/revise historical variants may remain as
-- inactive audit evidence without blocking the selected output.
CREATE OR REPLACE FUNCTION public.df_v3_validate_studio_stage_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state='generating' THEN
    IF EXISTS (
      SELECT 1
      FROM public.v3_studio_stage_dependencies d
      JOIN public.v3_studio_stage_runs p ON p.stage_run_id=d.parent_stage_run_id
      WHERE d.child_stage_run_id=NEW.stage_run_id AND p.state<>'approved'
    ) THEN
      RAISE EXCEPTION 'v3_studio_stage_dependencies_not_approved:stage=%', NEW.stage_run_id;
    END IF;
  END IF;

  IF NEW.state='approved' THEN
    IF NOT EXISTS (
      SELECT 1 FROM public.v3_studio_stage_outputs o
      WHERE o.stage_run_id=NEW.stage_run_id AND o.is_active=true
    ) THEN
      RAISE EXCEPTION 'v3_studio_stage_approval_requires_active_output:stage=%', NEW.stage_run_id;
    END IF;
    IF EXISTS (
      SELECT 1
      FROM public.v3_studio_stage_outputs o
      LEFT JOIN public.v3_studio_review_items r
        ON r.stage_run_id=o.stage_run_id AND r.media_id=o.media_id
      WHERE o.stage_run_id=NEW.stage_run_id
        AND o.is_active=true
        AND COALESCE(r.decision,'pending')<>'approved'
    ) THEN
      RAISE EXCEPTION 'v3_studio_stage_approval_requires_all_active_outputs_approved:stage=%', NEW.stage_run_id;
    END IF;
  END IF;
  NEW.updated_at=now();
  RETURN NEW;
END;
$$;

COMMIT;
