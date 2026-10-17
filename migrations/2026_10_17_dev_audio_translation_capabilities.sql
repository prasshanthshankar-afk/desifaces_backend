-- DEV-only additive translation capability catalog. NOT auto-applied.
-- Existing TTS tables and all pricing/payment tables are untouched.
BEGIN;
CREATE TABLE IF NOT EXISTS public.audio_translation_capabilities (
  provider_code text NOT NULL,
  model_code text NOT NULL,
  source_locale text NOT NULL,
  target_locale text NOT NULL,
  provider_source_code text NOT NULL,
  provider_target_code text NOT NULL,
  priority integer NOT NULL DEFAULT 100,
  is_enabled boolean NOT NULL DEFAULT false,
  is_approved boolean NOT NULL DEFAULT false,
  evidence_ref text,
  meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider_code, model_code, source_locale, target_locale),
  CONSTRAINT audio_translation_no_identity CHECK (source_locale <> target_locale),
  CONSTRAINT audio_translation_nonempty CHECK (
    length(btrim(provider_code)) > 0 AND length(btrim(model_code)) > 0
    AND length(btrim(source_locale)) > 0 AND length(btrim(target_locale)) > 0
    AND length(btrim(provider_source_code)) > 0 AND length(btrim(provider_target_code)) > 0
  ),
  CONSTRAINT audio_translation_source_locale_fk FOREIGN KEY (source_locale)
    REFERENCES public.tts_locales(locale) ON DELETE RESTRICT,
  CONSTRAINT audio_translation_target_locale_fk FOREIGN KEY (target_locale)
    REFERENCES public.tts_locales(locale) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_audio_translation_eligible
 ON public.audio_translation_capabilities (source_locale,target_locale,priority)
 WHERE is_enabled AND is_approved;
-- Intentionally no provider capability rows: live API verification must precede approval.
COMMIT;
