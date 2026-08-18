-- V3-C5: Canonical generation request/job/provider persistence.
--
-- This layer is additive beside existing studio_jobs/face/audio/fusion tables.
-- It is the persistence target for new V3 capabilities and compatibility adapters.
-- Existing V2 jobs are not rewritten or re-executed by this migration.

BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.v3_generation_requests (
  generation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE RESTRICT,
  requested_by_user_id uuid NOT NULL,
  project_id uuid,
  generation_kind text NOT NULL,
  participant_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  source_media_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  parameters_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  pricing_quote_id uuid,
  safety_state text NOT NULL DEFAULT 'pending',
  idempotency_key text NOT NULL,
  request_digest text NOT NULL,
  request_context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  compatibility_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_generation_kind_nonempty CHECK (length(btrim(generation_kind)) > 0),
  CONSTRAINT ck_v3_generation_idempotency_nonempty CHECK (length(btrim(idempotency_key)) > 0),
  CONSTRAINT ck_v3_generation_request_digest_nonempty CHECK (length(btrim(request_digest)) > 0),
  CONSTRAINT ck_v3_generation_safety_state CHECK (safety_state IN ('pending','allowed','blocked','review_required')),
  UNIQUE(account_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_v3_generation_requests_user_created
  ON public.v3_generation_requests(requested_by_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_generation_requests_account_created
  ON public.v3_generation_requests(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_generation_requests_project_created
  ON public.v3_generation_requests(project_id, created_at DESC)
  WHERE project_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.v3_generation_jobs (
  job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  generation_id uuid NOT NULL REFERENCES public.v3_generation_requests(generation_id) ON DELETE CASCADE,
  parent_job_id uuid REFERENCES public.v3_generation_jobs(job_id) ON DELETE CASCADE,
  job_type text NOT NULL DEFAULT 'root',
  state text NOT NULL DEFAULT 'submitted',
  progress_percent integer,
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  available_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  heartbeat_at timestamptz,
  lease_owner text,
  lease_expires_at timestamptz,
  error_code text,
  error_message text,
  compatibility_service text,
  compatibility_job_id text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_generation_job_type_nonempty CHECK (length(btrim(job_type)) > 0),
  CONSTRAINT ck_v3_generation_job_state CHECK (state IN ('submitted','queued','running','succeeded','failed','blocked','canceled','expired')),
  CONSTRAINT ck_v3_generation_job_progress CHECK (progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)),
  CONSTRAINT ck_v3_generation_job_attempts CHECK (attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_generation_root_job
  ON public.v3_generation_jobs(generation_id)
  WHERE parent_job_id IS NULL AND job_type = 'root';
CREATE INDEX IF NOT EXISTS idx_v3_generation_jobs_claim
  ON public.v3_generation_jobs(state, available_at, created_at)
  WHERE state IN ('submitted','queued');
CREATE INDEX IF NOT EXISTS idx_v3_generation_jobs_parent
  ON public.v3_generation_jobs(parent_job_id, created_at)
  WHERE parent_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_v3_generation_jobs_compat
  ON public.v3_generation_jobs(compatibility_service, compatibility_job_id)
  WHERE compatibility_job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.v3_provider_executions (
  execution_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL REFERENCES public.v3_generation_jobs(job_id) ON DELETE CASCADE,
  provider text NOT NULL,
  capability text NOT NULL,
  model text,
  state text NOT NULL DEFAULT 'planned',
  provider_request_id text,
  attempt integer NOT NULL DEFAULT 1,
  idempotency_key text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_provider_execution_provider_nonempty CHECK (length(btrim(provider)) > 0),
  CONSTRAINT ck_v3_provider_execution_capability_nonempty CHECK (length(btrim(capability)) > 0),
  CONSTRAINT ck_v3_provider_execution_state CHECK (state IN ('planned','submitted','running','succeeded','failed','canceled')),
  CONSTRAINT ck_v3_provider_execution_attempt CHECK (attempt >= 1),
  UNIQUE(job_id, provider, capability, attempt)
);

CREATE INDEX IF NOT EXISTS idx_v3_provider_execution_provider_request
  ON public.v3_provider_executions(provider, provider_request_id)
  WHERE provider_request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.v3_generation_job_media (
  job_id uuid NOT NULL REFERENCES public.v3_generation_jobs(job_id) ON DELETE CASCADE,
  media_id uuid NOT NULL REFERENCES public.media_assets(id) ON DELETE RESTRICT,
  relation text NOT NULL,
  sequence_no integer NOT NULL DEFAULT 0,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(job_id, media_id, relation),
  CONSTRAINT ck_v3_generation_job_media_relation CHECK (relation IN ('input','intermediate','preview','output','thumbnail')),
  CONSTRAINT ck_v3_generation_job_media_sequence CHECK (sequence_no >= 0)
);

CREATE INDEX IF NOT EXISTS idx_v3_generation_job_media_media
  ON public.v3_generation_job_media(media_id, relation, created_at);

-- State transition audit is append-only. This lets operations and future
-- Director/Conversation orchestration explain exactly how a job evolved.
CREATE TABLE IF NOT EXISTS public.v3_generation_job_events (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL REFERENCES public.v3_generation_jobs(job_id) ON DELETE CASCADE,
  from_state text,
  to_state text NOT NULL,
  event_type text NOT NULL DEFAULT 'state_transition',
  actor_type text,
  actor_id text,
  request_id text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_v3_generation_job_events_job_created
  ON public.v3_generation_job_events(job_id, created_at, event_id);

-- Canonical state-machine enforcement. A retry cannot illegally resurrect a
-- terminal job. Retrying work is represented by another provider attempt or a
-- new child job, not by mutating succeeded/failed/canceled/expired backwards.
CREATE OR REPLACE FUNCTION public.df_v3_validate_generation_job_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.state = OLD.state THEN
    NEW.updated_at := now();
    RETURN NEW;
  END IF;

  IF NOT (
    (OLD.state = 'submitted' AND NEW.state IN ('queued','running','blocked','canceled','expired','failed')) OR
    (OLD.state = 'queued' AND NEW.state IN ('running','blocked','canceled','expired','failed')) OR
    (OLD.state = 'running' AND NEW.state IN ('succeeded','failed','blocked','canceled','expired','queued')) OR
    (OLD.state = 'blocked' AND NEW.state IN ('queued','canceled','expired','failed'))
  ) THEN
    RAISE EXCEPTION 'invalid_v3_generation_job_transition:%->%', OLD.state, NEW.state;
  END IF;

  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_df_v3_generation_job_transition ON public.v3_generation_jobs;
CREATE TRIGGER trg_df_v3_generation_job_transition
BEFORE UPDATE OF state ON public.v3_generation_jobs
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_generation_job_transition();

-- Link canonical MediaAssets to canonical jobs now that the generation table exists.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_media_assets_parent_generation_job') THEN
    ALTER TABLE public.media_assets
      ADD CONSTRAINT fk_media_assets_parent_generation_job
      FOREIGN KEY (parent_generation_job_id) REFERENCES public.v3_generation_jobs(job_id) ON DELETE SET NULL;
  END IF;
END $$;

-- A small read model for APIs/operations. Provider-specific details are kept in
-- provider executions rather than leaking into the generation request contract.
CREATE OR REPLACE VIEW public.v3_generation_job_summary AS
SELECT
  j.job_id,
  j.generation_id,
  r.account_id,
  r.requested_by_user_id,
  r.project_id,
  r.generation_kind,
  r.participant_ids,
  r.source_media_ids,
  r.pricing_quote_id,
  r.safety_state,
  r.idempotency_key,
  j.parent_job_id,
  j.job_type,
  j.state,
  j.progress_percent,
  j.attempt_count,
  j.max_attempts,
  j.error_code,
  j.error_message,
  j.compatibility_service,
  j.compatibility_job_id,
  j.created_at,
  j.updated_at
FROM public.v3_generation_jobs j
JOIN public.v3_generation_requests r ON r.generation_id = j.generation_id;

COMMIT;
