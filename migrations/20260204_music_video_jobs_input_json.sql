BEGIN;

-- Safety: don't hang forever if something else is locking the table
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.music_video_jobs
  ADD COLUMN IF NOT EXISTS input_json jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMIT;