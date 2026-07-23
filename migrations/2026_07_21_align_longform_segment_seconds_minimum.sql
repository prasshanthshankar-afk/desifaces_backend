-- desifaces fusion-extension
-- Align longform_jobs.segment_seconds with the API/domain contract.
--
-- Current API/domain behavior supports 1..120 seconds and talking-video
-- requests intentionally use the resolved audio duration as segment_seconds.
-- The existing database constraint (5..120) rejects valid 1..4 second jobs.
--
-- This migration only broadens the lower bound from 5 to 1.
-- It does not alter pricing, planning, worker behavior, provider routing,
-- status transitions, or any existing rows.

BEGIN;

ALTER TABLE public.longform_jobs
    DROP CONSTRAINT IF EXISTS chk_longform_jobs_segment_seconds;

ALTER TABLE public.longform_jobs
    ADD CONSTRAINT chk_longform_jobs_segment_seconds
    CHECK (segment_seconds >= 1 AND segment_seconds <= 120);

COMMIT;