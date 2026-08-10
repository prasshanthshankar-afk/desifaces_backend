-- 20260426_fix_studio_coach_refresh_runs_status.sql
-- Production-safe, idempotent migration for Studio Coach refresh worker.
-- Root cause: worker creates a refresh run with status='running', but the existing
-- studio_coach_refresh_runs_status_check constraint rejects that value.

BEGIN;

-- Ensure the refresh run lifecycle supports an in-flight state.
ALTER TABLE IF EXISTS public.studio_coach_refresh_runs
  DROP CONSTRAINT IF EXISTS studio_coach_refresh_runs_status_check;

ALTER TABLE IF EXISTS public.studio_coach_refresh_runs
  ADD CONSTRAINT studio_coach_refresh_runs_status_check
  CHECK (
    status IN (
      'queued',
      'running',
      'succeeded',
      'failed',
      'skipped',
      'partial'
    )
  );

COMMIT;

-- Verification:
-- SELECT conname, pg_get_constraintdef(oid)
-- FROM pg_constraint
-- WHERE conrelid = 'public.studio_coach_refresh_runs'::regclass
--   AND conname = 'studio_coach_refresh_runs_status_check';
