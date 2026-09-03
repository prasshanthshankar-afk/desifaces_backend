-- desifaces-v3 durable Creative Director queue + Face -> Audio -> Fusion HITL workflow.
-- Downstream studio stages may consume only approved upstream outputs.

BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.v3_director_runs (
  run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id text NOT NULL UNIQUE,
  account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'queued',
  brief_json jsonb NOT NULL,
  resume_json jsonb,
  project_id uuid REFERENCES public.v3_projects(project_id) ON DELETE SET NULL,
  story_id uuid REFERENCES public.v3_stories(story_id) ON DELETE SET NULL,
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  available_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  lease_expires_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_director_run_state CHECK (state IN ('queued','running','awaiting_review','ready','failed','canceled')),
  CONSTRAINT ck_v3_director_run_attempts CHECK (attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts)
);
CREATE INDEX IF NOT EXISTS idx_v3_director_runs_claim
  ON public.v3_director_runs(state, available_at, created_at)
  WHERE state='queued';
CREATE INDEX IF NOT EXISTS idx_v3_director_runs_account_updated
  ON public.v3_director_runs(account_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.v3_studio_workflows (
  workflow_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL,
  owner_user_id uuid NOT NULL,
  project_id uuid NOT NULL,
  story_id uuid,
  state text NOT NULL DEFAULT 'draft',
  current_stage text,
  final_media_id uuid REFERENCES public.media_assets(id) ON DELETE SET NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fk_v3_studio_workflow_project_account
    FOREIGN KEY(project_id, account_id) REFERENCES public.v3_projects(project_id, account_id) ON DELETE CASCADE,
  CONSTRAINT fk_v3_studio_workflow_story
    FOREIGN KEY(story_id) REFERENCES public.v3_stories(story_id) ON DELETE CASCADE,
  CONSTRAINT ck_v3_studio_workflow_state CHECK (state IN ('draft','active','awaiting_review','completed','failed','canceled')),
  CONSTRAINT ck_v3_studio_workflow_current_stage CHECK (current_stage IS NULL OR current_stage IN ('face','audio','fusion','story_final'))
);
CREATE INDEX IF NOT EXISTS idx_v3_studio_workflows_account_updated
  ON public.v3_studio_workflows(account_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_studio_workflows_story
  ON public.v3_studio_workflows(story_id, updated_at DESC) WHERE story_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.v3_studio_stage_runs (
  stage_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id uuid NOT NULL REFERENCES public.v3_studio_workflows(workflow_id) ON DELETE CASCADE,
  stage_type text NOT NULL,
  scope_type text NOT NULL,
  participant_id uuid REFERENCES public.v3_participants(participant_id) ON DELETE RESTRICT,
  scene_id uuid REFERENCES public.v3_scenes(scene_id) ON DELETE RESTRICT,
  dialogue_turn_id uuid REFERENCES public.v3_dialogue_turns(turn_id) ON DELETE RESTRICT,
  state text NOT NULL DEFAULT 'pending',
  generation_request_id uuid REFERENCES public.v3_generation_requests(generation_id) ON DELETE SET NULL,
  generation_job_id uuid REFERENCES public.v3_generation_jobs(job_id) ON DELETE SET NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_studio_stage_type CHECK (stage_type IN ('face','audio','fusion','story_final')),
  CONSTRAINT ck_v3_studio_scope_type CHECK (scope_type IN ('participant','dialogue_turn','scene','story')),
  CONSTRAINT ck_v3_studio_stage_state CHECK (state IN ('pending','ready','generating','awaiting_review','approved','rejected','failed','skipped')),
  CONSTRAINT ck_v3_studio_stage_scope CHECK (
    (scope_type='participant' AND participant_id IS NOT NULL) OR
    (scope_type='dialogue_turn' AND dialogue_turn_id IS NOT NULL) OR
    (scope_type='scene' AND scene_id IS NOT NULL) OR
    (scope_type='story')
  )
);
CREATE INDEX IF NOT EXISTS idx_v3_studio_stage_workflow
  ON public.v3_studio_stage_runs(workflow_id, stage_type, created_at);

CREATE TABLE IF NOT EXISTS public.v3_studio_stage_dependencies (
  parent_stage_run_id uuid NOT NULL REFERENCES public.v3_studio_stage_runs(stage_run_id) ON DELETE CASCADE,
  child_stage_run_id uuid NOT NULL REFERENCES public.v3_studio_stage_runs(stage_run_id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(parent_stage_run_id, child_stage_run_id),
  CONSTRAINT ck_v3_studio_dependency_self CHECK (parent_stage_run_id <> child_stage_run_id)
);
CREATE INDEX IF NOT EXISTS idx_v3_studio_stage_dependencies_child
  ON public.v3_studio_stage_dependencies(child_stage_run_id, parent_stage_run_id);

CREATE TABLE IF NOT EXISTS public.v3_studio_stage_inputs (
  stage_run_id uuid NOT NULL REFERENCES public.v3_studio_stage_runs(stage_run_id) ON DELETE CASCADE,
  media_id uuid NOT NULL REFERENCES public.media_assets(id) ON DELETE RESTRICT,
  input_role text NOT NULL,
  source_stage_run_id uuid REFERENCES public.v3_studio_stage_runs(stage_run_id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(stage_run_id, media_id, input_role),
  CONSTRAINT ck_v3_studio_input_role_nonempty CHECK (length(btrim(input_role)) > 0)
);

CREATE TABLE IF NOT EXISTS public.v3_studio_stage_outputs (
  stage_run_id uuid NOT NULL REFERENCES public.v3_studio_stage_runs(stage_run_id) ON DELETE CASCADE,
  media_id uuid NOT NULL REFERENCES public.media_assets(id) ON DELETE RESTRICT,
  output_role text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(stage_run_id, media_id, output_role),
  CONSTRAINT ck_v3_studio_output_role_nonempty CHECK (length(btrim(output_role)) > 0)
);

CREATE TABLE IF NOT EXISTS public.v3_studio_review_items (
  review_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage_run_id uuid NOT NULL REFERENCES public.v3_studio_stage_runs(stage_run_id) ON DELETE CASCADE,
  media_id uuid NOT NULL REFERENCES public.media_assets(id) ON DELETE RESTRICT,
  decision text NOT NULL DEFAULT 'pending',
  reviewer_user_id uuid,
  feedback text,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_studio_review_decision CHECK (decision IN ('pending','approved','rejected','revise')),
  CONSTRAINT ck_v3_studio_review_decision_fields CHECK (
    (decision='pending' AND reviewer_user_id IS NULL AND decided_at IS NULL) OR
    (decision<>'pending' AND reviewer_user_id IS NOT NULL AND decided_at IS NOT NULL)
  ),
  UNIQUE(stage_run_id, media_id)
);
CREATE INDEX IF NOT EXISTS idx_v3_studio_review_pending
  ON public.v3_studio_review_items(stage_run_id, created_at) WHERE decision='pending';

CREATE OR REPLACE FUNCTION public.df_v3_validate_studio_workflow_story()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.story_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.v3_stories s
    WHERE s.story_id=NEW.story_id AND s.project_id=NEW.project_id AND s.account_id=NEW.account_id
  ) THEN
    RAISE EXCEPTION 'v3_studio_workflow_story_project_account_mismatch:workflow=% story=%', NEW.workflow_id, NEW.story_id;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_studio_workflow_story ON public.v3_studio_workflows;
CREATE TRIGGER trg_df_v3_studio_workflow_story
BEFORE INSERT OR UPDATE OF account_id,project_id,story_id ON public.v3_studio_workflows
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_studio_workflow_story();

CREATE OR REPLACE FUNCTION public.df_v3_validate_studio_stage_scope()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_project_id uuid;
  v_story_id uuid;
  v_scope_story uuid;
BEGIN
  SELECT project_id,story_id INTO v_project_id,v_story_id
  FROM public.v3_studio_workflows WHERE workflow_id=NEW.workflow_id;

  IF NEW.participant_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.v3_participants p WHERE p.participant_id=NEW.participant_id AND p.project_id=v_project_id
  ) THEN
    RAISE EXCEPTION 'v3_studio_stage_participant_project_mismatch:stage=% participant=%', NEW.stage_run_id, NEW.participant_id;
  END IF;

  IF NEW.scene_id IS NOT NULL THEN
    SELECT story_id INTO v_scope_story FROM public.v3_scenes WHERE scene_id=NEW.scene_id;
    IF v_scope_story IS NULL OR v_story_id IS NULL OR v_scope_story<>v_story_id THEN
      RAISE EXCEPTION 'v3_studio_stage_scene_story_mismatch:stage=% scene=%', NEW.stage_run_id, NEW.scene_id;
    END IF;
  END IF;

  IF NEW.dialogue_turn_id IS NOT NULL THEN
    SELECT sc.story_id INTO v_scope_story
    FROM public.v3_dialogue_turns dt JOIN public.v3_scenes sc ON sc.scene_id=dt.scene_id
    WHERE dt.turn_id=NEW.dialogue_turn_id;
    IF v_scope_story IS NULL OR v_story_id IS NULL OR v_scope_story<>v_story_id THEN
      RAISE EXCEPTION 'v3_studio_stage_turn_story_mismatch:stage=% turn=%', NEW.stage_run_id, NEW.dialogue_turn_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_studio_stage_scope ON public.v3_studio_stage_runs;
CREATE TRIGGER trg_df_v3_studio_stage_scope
BEFORE INSERT OR UPDATE OF workflow_id,participant_id,scene_id,dialogue_turn_id ON public.v3_studio_stage_runs
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_studio_stage_scope();

CREATE OR REPLACE FUNCTION public.df_v3_validate_stage_dependency()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_parent_workflow uuid;
  v_child_workflow uuid;
BEGIN
  SELECT workflow_id INTO v_parent_workflow FROM public.v3_studio_stage_runs WHERE stage_run_id=NEW.parent_stage_run_id;
  SELECT workflow_id INTO v_child_workflow FROM public.v3_studio_stage_runs WHERE stage_run_id=NEW.child_stage_run_id;
  IF v_parent_workflow IS NULL OR v_child_workflow IS NULL OR v_parent_workflow<>v_child_workflow THEN
    RAISE EXCEPTION 'v3_studio_dependency_cross_workflow';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_studio_stage_dependency ON public.v3_studio_stage_dependencies;
CREATE TRIGGER trg_df_v3_studio_stage_dependency
BEFORE INSERT OR UPDATE ON public.v3_studio_stage_dependencies
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_stage_dependency();

CREATE OR REPLACE FUNCTION public.df_v3_validate_stage_input_approved()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.source_stage_run_id IS NOT NULL THEN
    IF NOT EXISTS (
      SELECT 1 FROM public.v3_studio_stage_outputs o
      JOIN public.v3_studio_review_items r
        ON r.stage_run_id=o.stage_run_id AND r.media_id=o.media_id
      WHERE o.stage_run_id=NEW.source_stage_run_id
        AND o.media_id=NEW.media_id
        AND r.decision='approved'
    ) THEN
      RAISE EXCEPTION 'v3_studio_input_requires_approved_upstream_output:stage=% media=% source=%', NEW.stage_run_id, NEW.media_id, NEW.source_stage_run_id;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_studio_stage_input_approved ON public.v3_studio_stage_inputs;
CREATE TRIGGER trg_df_v3_studio_stage_input_approved
BEFORE INSERT OR UPDATE OF media_id,source_stage_run_id ON public.v3_studio_stage_inputs
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_stage_input_approved();

CREATE OR REPLACE FUNCTION public.df_v3_validate_review_output()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM public.v3_studio_stage_outputs o
    WHERE o.stage_run_id=NEW.stage_run_id AND o.media_id=NEW.media_id
  ) THEN
    RAISE EXCEPTION 'v3_studio_review_requires_stage_output:stage=% media=%', NEW.stage_run_id, NEW.media_id;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_studio_review_output ON public.v3_studio_review_items;
CREATE TRIGGER trg_df_v3_studio_review_output
BEFORE INSERT OR UPDATE OF stage_run_id,media_id ON public.v3_studio_review_items
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_review_output();

CREATE OR REPLACE FUNCTION public.df_v3_validate_studio_stage_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state='generating' THEN
    IF EXISTS (
      SELECT 1 FROM public.v3_studio_stage_dependencies d
      JOIN public.v3_studio_stage_runs p ON p.stage_run_id=d.parent_stage_run_id
      WHERE d.child_stage_run_id=NEW.stage_run_id AND p.state<>'approved'
    ) THEN
      RAISE EXCEPTION 'v3_studio_stage_dependencies_not_approved:stage=%', NEW.stage_run_id;
    END IF;
  END IF;

  IF NEW.state='approved' THEN
    IF NOT EXISTS (SELECT 1 FROM public.v3_studio_stage_outputs o WHERE o.stage_run_id=NEW.stage_run_id) THEN
      RAISE EXCEPTION 'v3_studio_stage_approval_requires_output:stage=%', NEW.stage_run_id;
    END IF;
    IF EXISTS (
      SELECT 1 FROM public.v3_studio_stage_outputs o
      LEFT JOIN public.v3_studio_review_items r
        ON r.stage_run_id=o.stage_run_id AND r.media_id=o.media_id
      WHERE o.stage_run_id=NEW.stage_run_id AND COALESCE(r.decision,'pending')<>'approved'
    ) THEN
      RAISE EXCEPTION 'v3_studio_stage_approval_requires_all_outputs_approved:stage=%', NEW.stage_run_id;
    END IF;
  END IF;
  NEW.updated_at=now();
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_studio_stage_transition ON public.v3_studio_stage_runs;
CREATE TRIGGER trg_df_v3_studio_stage_transition
BEFORE UPDATE OF state ON public.v3_studio_stage_runs
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_studio_stage_transition();

COMMIT;
