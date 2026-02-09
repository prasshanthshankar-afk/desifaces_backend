BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE public.music_tracks
  ADD COLUMN IF NOT EXISTS updated_at timestamp with time zone NOT NULL DEFAULT now();

-- Backfill (for rows created before this column existed)
UPDATE public.music_tracks
SET updated_at = created_at
WHERE updated_at IS NULL;

COMMIT;