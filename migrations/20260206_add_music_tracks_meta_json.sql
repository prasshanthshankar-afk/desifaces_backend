BEGIN;

ALTER TABLE public.music_tracks
  ADD COLUMN IF NOT EXISTS meta_json jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Optional: helpful if you ever query by keys (not required now)
CREATE INDEX IF NOT EXISTS music_tracks_meta_gin_idx
ON public.music_tracks USING gin (meta_json);

COMMIT;