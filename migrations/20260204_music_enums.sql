BEGIN;

-- 1) Create enum types (lowercase)
DO $$
BEGIN
  CREATE TYPE public.music_project_mode AS ENUM ('autopilot','co_create','byo');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
  CREATE TYPE public.music_track_type AS ENUM (
    'instrumental','vocals','full_mix','stems_zip','lyrics_json','timed_lyrics_json','cover_art'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
  CREATE TYPE public.music_performer_role AS ENUM (
    'lead','harmony','rap','backing','adlib','narration'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 2) Drop known bad check constraint(s) if present
ALTER TABLE IF EXISTS public.music_projects
  DROP CONSTRAINT IF EXISTS music_projects_mode_check;

-- If you have similar constraints on other tables, these won't error if they don't exist:
ALTER TABLE IF EXISTS public.music_tracks
  DROP CONSTRAINT IF EXISTS music_tracks_track_type_check;

ALTER TABLE IF EXISTS public.music_performers
  DROP CONSTRAINT IF EXISTS music_performers_role_check;

-- 3) Normalize existing string values -> lowercase canonical values
-- music_projects.mode
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='music_projects' AND column_name='mode'
  ) THEN
    UPDATE public.music_projects
    SET mode = CASE lower(mode::text)
      WHEN 'autopilot' THEN 'autopilot'
      WHEN 'auto' THEN 'autopilot'
      WHEN 'co_create' THEN 'co_create'
      WHEN 'cocreate' THEN 'co_create'
      WHEN 'co-create' THEN 'co_create'
      WHEN 'guided' THEN 'co_create'
      WHEN 'byo' THEN 'byo'
      WHEN 'bring_your_own' THEN 'byo'
      WHEN 'bring-your-own' THEN 'byo'
      WHEN 'upload' THEN 'byo'
      ELSE 'autopilot'
    END
    WHERE mode IS NOT NULL;
  END IF;
END $$;

-- music_tracks.track_type
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='music_tracks' AND column_name='track_type'
  ) THEN
    UPDATE public.music_tracks
    SET track_type = CASE lower(track_type::text)
      WHEN 'instrumental' THEN 'instrumental'
      WHEN 'inst' THEN 'instrumental'
      WHEN 'vocals' THEN 'vocals'
      WHEN 'vocal' THEN 'vocals'
      WHEN 'full_mix' THEN 'full_mix'
      WHEN 'full' THEN 'full_mix'
      WHEN 'mix' THEN 'full_mix'
      WHEN 'stems' THEN 'stems_zip'
      WHEN 'stems_zip' THEN 'stems_zip'
      WHEN 'lyrics' THEN 'lyrics_json'
      WHEN 'lyrics_json' THEN 'lyrics_json'
      WHEN 'timed_lyrics' THEN 'timed_lyrics_json'
      WHEN 'timed_lyrics_json' THEN 'timed_lyrics_json'
      WHEN 'cover' THEN 'cover_art'
      WHEN 'cover_art' THEN 'cover_art'
      ELSE 'full_mix'
    END
    WHERE track_type IS NOT NULL;
  END IF;
END $$;

-- music_performers.role
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='music_performers' AND column_name='role'
  ) THEN
    UPDATE public.music_performers
    SET role = CASE lower(role::text)
      WHEN 'lead' THEN 'lead'
      WHEN 'main' THEN 'lead'
      WHEN 'harmony' THEN 'harmony'
      WHEN 'chorus' THEN 'backing'
      WHEN 'backing' THEN 'backing'
      WHEN 'rap' THEN 'rap'
      WHEN 'adlib' THEN 'adlib'
      WHEN 'adlibs' THEN 'adlib'
      WHEN 'narration' THEN 'narration'
      WHEN 'narrator' THEN 'narration'
      ELSE 'lead'
    END
    WHERE role IS NOT NULL;
  END IF;
END $$;

-- 4) Convert columns to enum types (only if column exists)
DO $$
DECLARE v_udt text;
BEGIN
  SELECT udt_name INTO v_udt
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='music_projects' AND column_name='mode';

  IF v_udt IS NOT NULL AND v_udt <> 'music_project_mode' THEN
    EXECUTE $q$
      ALTER TABLE public.music_projects
        ALTER COLUMN mode TYPE public.music_project_mode
        USING lower(mode::text)::public.music_project_mode
    $q$;
    EXECUTE $q$
      ALTER TABLE public.music_projects
        ALTER COLUMN mode SET DEFAULT 'autopilot'
    $q$;
  END IF;
END $$;

DO $$
DECLARE v_udt text;
BEGIN
  SELECT udt_name INTO v_udt
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='music_tracks' AND column_name='track_type';

  IF v_udt IS NOT NULL AND v_udt <> 'music_track_type' THEN
    EXECUTE $q$
      ALTER TABLE public.music_tracks
        ALTER COLUMN track_type TYPE public.music_track_type
        USING lower(track_type::text)::public.music_track_type
    $q$;
  END IF;
END $$;

DO $$
DECLARE v_udt text;
BEGIN
  SELECT udt_name INTO v_udt
  FROM information_schema.columns
  WHERE table_schema='public' AND table_name='music_performers' AND column_name='role';

  IF v_udt IS NOT NULL AND v_udt <> 'music_performer_role' THEN
    EXECUTE $q$
      ALTER TABLE public.music_performers
        ALTER COLUMN role TYPE public.music_performer_role
        USING lower(role::text)::public.music_performer_role
    $q$;
    EXECUTE $q$
      ALTER TABLE public.music_performers
        ALTER COLUMN role SET DEFAULT 'lead'
    $q$;
  END IF;
END $$;

-- 5) Helpful index
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='music_tracks') THEN
    CREATE INDEX IF NOT EXISTS idx_music_tracks_project_created
      ON public.music_tracks (project_id, created_at DESC);
  END IF;
END $$;

COMMIT;