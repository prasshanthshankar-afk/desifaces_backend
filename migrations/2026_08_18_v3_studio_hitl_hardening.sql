-- Harden V3 staged Studio HITL workflow for variants, ownership and direct/story scopes.
BEGIN;

ALTER TABLE public.v3_studio_stage_outputs
  ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;
CREATE INDEX IF NOT EXISTS idx_v3_studio_stage_outputs_active
  ON public.v3_studio_stage_outputs(stage_run_id, created_at, media_id)
  WHERE is_active=true;

-- A scope has exactly the identifiers it needs and no unrelated IDs.
ALTER TABLE public.v3_studio_stage_runs DROP CONSTRAINT IF EXISTS ck_v3_studio_stage_scope;
ALTER TABLE public.v3_studio_stage_runs ADD CONSTRAINT ck_v3_studio_stage_scope CHECK (
  (scope_type='participant' AND participant_id IS NOT NULL AND scene_id IS NULL AND dialogue_turn_id IS NULL) OR
  (scope_type='dialogue_turn' AND participant_id IS NULL AND scene_id IS NULL AND dialogue_turn_id IS NOT NULL) OR
  (scope_type='scene' AND participant_id IS NULL AND scene_id IS NOT NULL AND dialogue_turn_id IS NULL) OR
  (scope_type='story' AND participant_id IS NULL AND scene_id IS NULL AND dialogue_turn_id IS NULL)
);

-- Preserve today's direct 1-person flow while supporting Story execution:
-- Face: participant; Audio: participant OR dialogue turn; Fusion: participant OR scene; final: story.
ALTER TABLE public.v3_studio_stage_runs DROP CONSTRAINT IF EXISTS ck_v3_studio_stage_type_scope;
ALTER TABLE public.v3_studio_stage_runs ADD CONSTRAINT ck_v3_studio_stage_type_scope CHECK (
  (stage_type='face' AND scope_type='participant') OR
  (stage_type='audio' AND scope_type IN ('participant','dialogue_turn')) OR
  (stage_type='fusion' AND scope_type IN ('participant','scene')) OR
  (stage_type='story_final' AND scope_type='story')
);

-- Every Studio artifact must stay inside the workflow billing account. If an
-- input comes from another stage, that stage must belong to this same workflow.
CREATE OR REPLACE FUNCTION public.df_v3_validate_studio_artifact()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_target_workflow uuid;
  v_target_account uuid;
  v_media_account uuid;
  v_source_workflow uuid;
BEGIN
  SELECT s.workflow_id,w.account_id INTO v_target_workflow,v_target_account
  FROM public.v3_studio_stage_runs s
  JOIN public.v3_studio_workflows w ON w.workflow_id=s.workflow_id
  WHERE s.stage_run_id=NEW.stage_run_id;
  SELECT account_id INTO v_media_account FROM public.media_assets WHERE id=NEW.media_id;
  IF v_target_workflow IS NULL OR v_media_account IS NULL OR v_target_account<>v_media_account THEN
    RAISE EXCEPTION 'v3_studio_artifact_account_mismatch:stage=% media=%', NEW.stage_run_id, NEW.media_id;
  END IF;
  IF TG_TABLE_NAME='v3_studio_stage_inputs' AND NEW.source_stage_run_id IS NOT NULL THEN
    SELECT workflow_id INTO v_source_workflow
    FROM public.v3_studio_stage_runs WHERE stage_run_id=NEW.source_stage_run_id;
    IF v_source_workflow IS NULL OR v_source_workflow<>v_target_workflow THEN
      RAISE EXCEPTION 'v3_studio_input_cross_workflow:stage=% source=%', NEW.stage_run_id, NEW.source_stage_run_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_studio_input_artifact ON public.v3_studio_stage_inputs;
CREATE TRIGGER trg_df_v3_studio_input_artifact
BEFORE INSERT OR UPDATE OF stage_run_id,media_id,source_stage_run_id ON public.v3_studio_stage_inputs
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_studio_artifact();
DROP TRIGGER IF EXISTS trg_df_v3_studio_output_artifact ON public.v3_studio_stage_outputs;
CREATE TRIGGER trg_df_v3_studio_output_artifact
BEFORE INSERT OR UPDATE OF stage_run_id,media_id ON public.v3_studio_stage_outputs
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_studio_artifact();

-- Downstream input must be an ACTIVE approved output from the referenced stage.
CREATE OR REPLACE FUNCTION public.df_v3_validate_stage_input_approved()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source_stage_run_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.v3_studio_stage_outputs o
    JOIN public.v3_studio_review_items r
      ON r.stage_run_id=o.stage_run_id AND r.media_id=o.media_id
    WHERE o.stage_run_id=NEW.source_stage_run_id AND o.media_id=NEW.media_id
      AND o.is_active=true AND r.decision='approved'
  ) THEN
    RAISE EXCEPTION 'v3_studio_input_requires_active_approved_upstream_output:stage=% media=% source=%',
      NEW.stage_run_id, NEW.media_id, NEW.source_stage_run_id;
  END IF;
  RETURN NEW;
END;
$$;

-- Stage approval requires at least one active output and every active output to
-- be approved. Rejected/revise historical variants remain inactive audit evidence.
CREATE OR REPLACE FUNCTION public.df_v3_validate_studio_stage_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state='generating' AND EXISTS (
    SELECT 1 FROM public.v3_studio_stage_dependencies d
    JOIN public.v3_studio_stage_runs p ON p.stage_run_id=d.parent_stage_run_id
    WHERE d.child_stage_run_id=NEW.stage_run_id AND p.state<>'approved'
  ) THEN
    RAISE EXCEPTION 'v3_studio_stage_dependencies_not_approved:stage=%', NEW.stage_run_id;
  END IF;

  IF NEW.state='approved' THEN
    IF NOT EXISTS (
      SELECT 1 FROM public.v3_studio_stage_outputs o
      WHERE o.stage_run_id=NEW.stage_run_id AND o.is_active=true
    ) THEN
      RAISE EXCEPTION 'v3_studio_stage_approval_requires_active_output:stage=%', NEW.stage_run_id;
    END IF;
    IF EXISTS (
      SELECT 1 FROM public.v3_studio_stage_outputs o
      LEFT JOIN public.v3_studio_review_items r
        ON r.stage_run_id=o.stage_run_id AND r.media_id=o.media_id
      WHERE o.stage_run_id=NEW.stage_run_id AND o.is_active=true
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
