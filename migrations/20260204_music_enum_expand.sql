BEGIN;

-- Prevent deploy from hanging indefinitely on enum locks
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_enum e
    JOIN pg_type t ON t.oid = e.enumtypid
    WHERE t.typname = 'music_project_mode' AND e.enumlabel = 'autopilot'
  ) THEN
    ALTER TYPE public.music_project_mode ADD VALUE 'autopilot';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_enum e
    JOIN pg_type t ON t.oid = e.enumtypid
    WHERE t.typname = 'music_project_mode' AND e.enumlabel = 'co_create'
  ) THEN
    ALTER TYPE public.music_project_mode ADD VALUE 'co_create';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_enum e
    JOIN pg_type t ON t.oid = e.enumtypid
    WHERE t.typname = 'music_project_mode' AND e.enumlabel = 'byo'
  ) THEN
    ALTER TYPE public.music_project_mode ADD VALUE 'byo';
  END IF;
END $$;

DO $$
DECLARE v text;
BEGIN
  FOREACH v IN ARRAY ARRAY[
    'instrumental','vocals','full_mix','stems_zip','lyrics_json','timed_lyrics_json','cover_art'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_enum e
      JOIN pg_type t ON t.oid = e.enumtypid
      WHERE t.typname = 'music_track_type' AND e.enumlabel = v
    ) THEN
      EXECUTE format('ALTER TYPE public.music_track_type ADD VALUE %L', v);
    END IF;
  END LOOP;
END $$;

DO $$
DECLARE v text;
BEGIN
  FOREACH v IN ARRAY ARRAY[
    'lead','harmony','rap','backing','adlib','narration'
  ]
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_enum e
      JOIN pg_type t ON t.oid = e.enumtypid
      WHERE t.typname = 'music_performer_role' AND e.enumlabel = v
    ) THEN
      EXECUTE format('ALTER TYPE public.music_performer_role ADD VALUE %L', v);
    END IF;
  END LOOP;
END $$;

COMMIT