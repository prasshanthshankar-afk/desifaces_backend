BEGIN;

ALTER TABLE public.tts_voice_locale_capabilities
ADD COLUMN IF NOT EXISTS selection_priority integer NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tts_voice_locale_selection
ON public.tts_voice_locale_capabilities(
    locale,
    is_enabled,
    is_approved,
    is_recommended,
    selection_priority DESC,
    quality_score DESC
);

UPDATE public.masterdata_revision
SET revision = GREATEST(revision, 10)
WHERE domain='tts';

COMMIT;
