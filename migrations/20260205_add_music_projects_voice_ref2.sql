BEGIN;

-- Ensure column exists
ALTER TABLE public.music_video_jobs
  ADD COLUMN IF NOT EXISTS input_json jsonb;

-- If input_json exists but is not jsonb, convert safely
DO \$\$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema='public'
      AND table_name='music_video_jobs'
      AND column_name='input_json'
      AND udt_name <> 'jsonb'
  ) THEN
    ALTER TABLE public.music_video_jobs
      ALTER COLUMN input_json TYPE jsonb
      USING COALESCE(NULLIF(btrim(input_json::text), '')::jsonb, '{}'::jsonb);
  END IF;
END \$\$;

ALTER TABLE public.music_video_jobs
  ALTER COLUMN input_json SET DEFAULT '{}'::jsonb;

UPDATE public.music_video_jobs
SET input_json = '{}'::jsonb
WHERE input_json IS NULL;

ALTER TABLE public.music_video_jobs
  ALTER COLUMN input_json SET NOT NULL;

COMMIT;