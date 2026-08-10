BEGIN;

INSERT INTO public.tts_model_locale_capabilities (
    provider_code,
    model_code,
    locale,
    provider_locale_code,
    accent_code,
    support_level,
    is_enabled,
    is_approved,
    source,
    source_version,
    last_seen_at,
    meta_json
)
SELECT
    ml.provider_code,
    ml.model_code,
    l.locale,
    NULL,
    NULLIF(l.accent_code, ''),
    ml.support_level,
    true,
    true,
    'derived_masterdata',
    '2026-08',
    now(),
    jsonb_build_object(
        'language_code', l.language_code,
        'provider_language_code', ml.provider_language_code
    )
FROM public.tts_model_language_capabilities ml
JOIN public.tts_locales l
  ON lower(l.language_code) = lower(ml.language_code)
WHERE ml.provider_code = 'elevenlabs'
  AND ml.is_enabled = true
  AND ml.is_approved = true
  AND l.is_enabled = true
  AND l.tts_supported = true
  AND l.is_user_selectable = true
ON CONFLICT (provider_code, model_code, locale)
DO UPDATE SET
    accent_code = EXCLUDED.accent_code,
    support_level = EXCLUDED.support_level,
    is_enabled = true,
    is_approved = true,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version,
    last_seen_at = now(),
    meta_json = EXCLUDED.meta_json,
    updated_at = now();

UPDATE public.masterdata_revision
SET revision = GREATEST(revision, 13)
WHERE domain = 'tts';

COMMIT;
