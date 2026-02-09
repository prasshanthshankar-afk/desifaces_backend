BEGIN;

-- 1) Revision table
CREATE TABLE IF NOT EXISTS public.masterdata_revision (
  domain     text PRIMARY KEY,           -- e.g. 'face', 'tts'
  revision   bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Ensure domain rows exist
INSERT INTO public.masterdata_revision(domain) VALUES ('face')
ON CONFLICT (domain) DO NOTHING;

INSERT INTO public.masterdata_revision(domain) VALUES ('tts')
ON CONFLICT (domain) DO NOTHING;

-- 2) Bump function (also notifies listeners)
CREATE OR REPLACE FUNCTION public.bump_masterdata_revision(p_domain text)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE public.masterdata_revision
     SET revision = revision + 1,
         updated_at = now()
   WHERE domain = p_domain;

  -- Optional: used for API cache invalidation across pods
  PERFORM pg_notify(
    'masterdata_changed',
    json_build_object('domain', p_domain)::text
  );
END;
$$;

-- 3) Trigger functions (statement triggers should RETURN NULL)
CREATE OR REPLACE FUNCTION public.masterdata_face_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM public.bump_masterdata_revision('face');
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION public.masterdata_tts_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM public.bump_masterdata_revision('tts');
  RETURN NULL;
END;
$$;

-- 4) Install triggers for FACE domain masterdata tables
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'face_generation_regions',
    'face_generation_contexts',
    'face_generation_use_cases',
    'face_generation_subject_compositions',
    'face_generation_clothing',
    'face_generation_features',
    'face_generation_skin_tones',
    'face_generation_age_ranges',
    'face_generation_image_formats',
    'face_generation_variations'
  ]
  LOOP
    IF EXISTS (
      SELECT 1
      FROM information_schema.tables
      WHERE table_schema='public'
        AND table_name=t
    ) THEN
      -- drop if exists
      EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_bump ON public.%I;', t, t);

      -- create
      EXECUTE format(
        'CREATE TRIGGER trg_%s_bump
           AFTER INSERT OR UPDATE OR DELETE ON public.%I
           FOR EACH STATEMENT
           EXECUTE FUNCTION public.masterdata_face_trigger();',
        t, t
      );
    END IF;
  END LOOP;
END $$;

-- 5) Install triggers for TTS domain masterdata tables
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'tts_locales',
    'tts_voices'
  ]
  LOOP
    IF EXISTS (
      SELECT 1
      FROM information_schema.tables
      WHERE table_schema='public'
        AND table_name=t
    ) THEN
      EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_bump ON public.%I;', t, t);

      EXECUTE format(
        'CREATE TRIGGER trg_%s_bump
           AFTER INSERT OR UPDATE OR DELETE ON public.%I
           FOR EACH STATEMENT
           EXECUTE FUNCTION public.masterdata_tts_trigger();',
        t, t
      );
    END IF;
  END LOOP;
END $$;

COMMIT;
