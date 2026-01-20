-- ============================================================
-- 0xx_tts_catalog.sql
-- Tables:
--   - tts_locales  (locale-level support flags + mapping)
--   - tts_voices   (voice inventory per locale)
-- ============================================================

BEGIN;

-- Keep consistent with your schema style (updated_at maintained).
CREATE OR REPLACE FUNCTION public.fn_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- ----------------------------
-- tts_locales
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.tts_locales (
  locale             text PRIMARY KEY,                -- e.g. hi-IN, ta-IN, en-IN, en-US, en-GB
  translator_lang    text,                            -- e.g. hi, ta, en (Translator uses base codes)
  tts_supported      boolean NOT NULL DEFAULT false,
  translate_supported boolean NOT NULL DEFAULT false,
  is_enabled         boolean NOT NULL DEFAULT true,    -- allow disabling in UI without deleting
  display_name       text,                            -- optional friendly name
  native_name        text,                            -- optional native name
  meta_json          jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_tts_locales_set_updated_at
BEFORE UPDATE ON public.tts_locales
FOR EACH ROW EXECUTE FUNCTION public.fn_set_updated_at();

CREATE INDEX IF NOT EXISTS idx_tts_locales_enabled
ON public.tts_locales (is_enabled);

CREATE INDEX IF NOT EXISTS idx_tts_locales_supported
ON public.tts_locales (tts_supported, translate_supported);

-- ----------------------------
-- tts_voices
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.tts_voices (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider          text NOT NULL DEFAULT 'azure',
  voice_name        text NOT NULL,                    -- e.g. hi-IN-SwaraNeural (Speech "ShortName")
  locale            text NOT NULL,                    -- e.g. hi-IN
  gender            text,                             -- Male/Female/Neutral
  voice_type        text,                             -- Neural/Standard etc (if available)
  is_default        boolean NOT NULL DEFAULT false,    -- one default per locale is recommended
  supports_styles   boolean NOT NULL DEFAULT false,
  meta_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_tts_voices_provider_voice UNIQUE (provider, voice_name),
  CONSTRAINT fk_tts_voices_locale FOREIGN KEY (locale)
    REFERENCES public.tts_locales(locale)
    ON DELETE RESTRICT
);

CREATE TRIGGER trg_tts_voices_set_updated_at
BEFORE UPDATE ON public.tts_voices
FOR EACH ROW EXECUTE FUNCTION public.fn_set_updated_at();

CREATE INDEX IF NOT EXISTS idx_tts_voices_locale
ON public.tts_voices (locale);

CREATE INDEX IF NOT EXISTS idx_tts_voices_locale_default
ON public.tts_voices (locale, is_default);

COMMIT;