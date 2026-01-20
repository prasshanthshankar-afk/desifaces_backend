-- ============================================================================
-- DesiFaces.ai - Fusion/Face Studio "Rock Solid" Hardening
-- Date: 2026-01-05
-- Purpose:
--   - Repeatable delivery (idempotent submit)
--   - Safe multi-worker claiming (SKIP LOCKED)
--   - Provider run uniqueness (avoid duplicate provider jobs)
--   - Queue performance indexes
--   - Optional retry scheduling columns (attempt_count, next_run_at)
--
-- Notes:
--   - This script is designed to be SAFE to re-run (IF NOT EXISTS guards).
--   - It assumes tables exist in schema "public".
--   - It does NOT drop anything.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1) studio_jobs: idempotent submit
--    Prevent duplicates for same user + studio_type + request_hash.
-- ----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_studio_jobs_user_type_request_hash
ON public.studio_jobs (user_id, studio_type, request_hash);

-- ----------------------------------------------------------------------------
-- 2) studio_jobs: queue performance
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_studio_jobs_queue
ON public.studio_jobs (studio_type, status, created_at);

-- ----------------------------------------------------------------------------
-- 3) Optional: retry scheduling (recommended)
--    If you already have these columns, the ALTERs do nothing.
-- ----------------------------------------------------------------------------
ALTER TABLE public.studio_jobs
  ADD COLUMN IF NOT EXISTS attempt_count int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS next_run_at timestamptz NOT NULL DEFAULT now();

-- Queue index that supports next_run_at scheduling
CREATE INDEX IF NOT EXISTS ix_studio_jobs_next_run
ON public.studio_jobs (studio_type, status, next_run_at, created_at);

-- ----------------------------------------------------------------------------
-- 4) provider_runs: idempotency + uniqueness
--    Ensure idempotency key is unique (one run per idem key).
-- ----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_runs_idempotency_key
ON public.provider_runs (idempotency_key);

-- Ensure provider + provider_job_id is unique when provider_job_id is present
CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_runs_provider_job_id
ON public.provider_runs (provider, provider_job_id)
WHERE provider_job_id IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 5) studio_job_steps: optional uniqueness for step attempts
--    Only run if your table has (job_id, step_code, attempt).
--    If column names differ, comment out and adjust.
-- ----------------------------------------------------------------------------
-- CREATE UNIQUE INDEX IF NOT EXISTS ux_studio_job_steps_job_step_attempt
-- ON public.studio_job_steps (job_id, step_code, attempt);

COMMIT;

-- ============================================================================
-- END
-- ============================================================================