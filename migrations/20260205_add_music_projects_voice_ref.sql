BEGIN;

-- 1) Column (this part you already ran successfully)
ALTER TABLE public.music_projects
  ADD COLUMN IF NOT EXISTS voice_ref_asset_id uuid;

-- 2) FK constraint (Postgres has no "IF NOT EXISTS" here, so we guard manually)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    WHERE c.conname = 'music_projects_voice_ref_asset_fk'
      AND c.conrelid = 'public.music_projects'::regclass
  ) THEN
    ALTER TABLE public.music_projects
      ADD CONSTRAINT music_projects_voice_ref_asset_fk
      FOREIGN KEY (voice_ref_asset_id)
      REFERENCES public.media_assets(id)
      ON DELETE SET NULL;
  END IF;
END $$;

-- 3) Index
CREATE INDEX IF NOT EXISTS idx_music_projects_voice_ref_asset_id
  ON public.music_projects(voice_ref_asset_id);

COMMIT;

BEGIN;

ALTER TABLE public.music_projects
  ADD COLUMN IF NOT EXISTS voice_ref_asset_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    WHERE c.conname = 'music_projects_voice_ref_asset_fk'
      AND c.conrelid = 'public.music_projects'::regclass
  ) THEN
    ALTER TABLE public.music_projects
      ADD CONSTRAINT music_projects_voice_ref_asset_fk
      FOREIGN KEY (voice_ref_asset_id)
      REFERENCES public.media_assets(id)
      ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_music_projects_voice_ref_asset_id
  ON public.music_projects(voice_ref_asset_id);

COMMIT;