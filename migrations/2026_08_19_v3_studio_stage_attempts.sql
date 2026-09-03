-- Durable execution-attempt history for independently billable/retryable Studio output slots.
-- Billing authority remains svc-pricing/credit ledger; this table records execution correlation.
-- Each attempt is also bound to the certified C5 canonical GenerationRequest/root GenerationJob.

BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.v3_studio_stage_attempts (
  attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  stage_run_id uuid NOT NULL REFERENCES public.v3_studio_stage_runs(stage_run_id) ON DELETE CASCADE,
  attempt_no integer NOT NULL,
  attempt_kind text NOT NULL,
  state text NOT NULL DEFAULT 'dispatching',
  generation_id uuid,
  generation_job_id uuid,
  provider_service text NOT NULL,
  provider_job_ref text,
  pricing_quote_id text,
  preview_fingerprint text,
  media_id uuid REFERENCES public.media_assets(id) ON DELETE SET NULL,
  error_code text,
  error_message text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_v3_studio_stage_attempt_no UNIQUE(stage_run_id, attempt_no),
  CONSTRAINT ck_v3_studio_stage_attempt_no CHECK (attempt_no >= 1),
  CONSTRAINT ck_v3_studio_stage_attempt_kind CHECK (attempt_kind IN ('initial','retry','regenerate')),
  CONSTRAINT ck_v3_studio_stage_attempt_state CHECK (
    state IN ('dispatching','queued','running','succeeded','failed','canceled')
  ),
  CONSTRAINT ck_v3_studio_stage_attempt_completed CHECK (
    (state IN ('succeeded','failed','canceled') AND completed_at IS NOT NULL) OR
    (state NOT IN ('succeeded','failed','canceled'))
  )
);

-- Upgrade an already-created attempt table idempotently.
ALTER TABLE public.v3_studio_stage_attempts
  ADD COLUMN IF NOT EXISTS generation_id uuid,
  ADD COLUMN IF NOT EXISTS generation_job_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='fk_v3_studio_attempt_generation'
      AND conrelid='public.v3_studio_stage_attempts'::regclass
  ) THEN
    ALTER TABLE public.v3_studio_stage_attempts
      ADD CONSTRAINT fk_v3_studio_attempt_generation
      FOREIGN KEY(generation_id)
      REFERENCES public.v3_generation_requests(generation_id)
      ON DELETE SET NULL;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='fk_v3_studio_attempt_generation_job'
      AND conrelid='public.v3_studio_stage_attempts'::regclass
  ) THEN
    ALTER TABLE public.v3_studio_stage_attempts
      ADD CONSTRAINT fk_v3_studio_attempt_generation_job
      FOREIGN KEY(generation_job_id)
      REFERENCES public.v3_generation_jobs(job_id)
      ON DELETE SET NULL;
  END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_studio_stage_attempt_generation
  ON public.v3_studio_stage_attempts(generation_id)
  WHERE generation_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_studio_stage_attempt_generation_job
  ON public.v3_studio_stage_attempts(generation_job_id)
  WHERE generation_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_v3_studio_stage_attempts_stage
  ON public.v3_studio_stage_attempts(stage_run_id, attempt_no DESC);
CREATE INDEX IF NOT EXISTS idx_v3_studio_stage_attempts_provider_job
  ON public.v3_studio_stage_attempts(provider_service, provider_job_ref)
  WHERE provider_job_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_studio_stage_attempt_provider_job
  ON public.v3_studio_stage_attempts(provider_service, provider_job_ref)
  WHERE provider_job_ref IS NOT NULL;

CREATE OR REPLACE FUNCTION public.df_v3_touch_studio_stage_attempt()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at=now();
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_touch_studio_stage_attempt ON public.v3_studio_stage_attempts;
CREATE TRIGGER trg_df_v3_touch_studio_stage_attempt
BEFORE UPDATE ON public.v3_studio_stage_attempts
FOR EACH ROW EXECUTE FUNCTION public.df_v3_touch_studio_stage_attempt();

COMMIT;
