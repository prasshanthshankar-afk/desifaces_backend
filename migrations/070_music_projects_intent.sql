BEGIN;

ALTER TABLE public.music_projects
  ADD COLUMN IF NOT EXISTS intent_text TEXT,
  ADD COLUMN IF NOT EXISTS intent_updated_at TIMESTAMPTZ;

-- Optional: small helper index for listing/filtering by mode + recency
CREATE INDEX IF NOT EXISTS music_projects_mode_intent_updated_idx
  ON public.music_projects (mode, intent_updated_at DESC);

COMMIT;