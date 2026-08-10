-- ============================================================
-- #eip7
-- Complete the global svc-audio SQL foundation.
--
-- Adds:
--   * generic global BCP-47 language locales
--   * global language aliases
--   * explicit voice <-> provider model capability
--
-- No provider routing decisions are encoded here.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. GENERIC GLOBAL LANGUAGE LOCALES
--
-- A language tag such as:
--   fr
--   es
--   ja
--   sw
--
-- is itself a valid BCP-47 language tag.
--
-- We deliberately DO NOT generate fake combinations such as every
-- language x every country.
--
-- Exact regional locales such as fr-FR, fr-CA, en-US, en-IN etc.
-- are added when provider catalogs establish real capability.
-- ------------------------------------------------------------

INSERT INTO public.tts_locales (
    locale,
    translator_lang,
    tts_supported,
    translate_supported,
    is_enabled,
    display_name,
    native_name,
    meta_json,
    language_code,
    region_code,
    country_code,
    script_code,
    accent_code,
    is_user_selectable,
    catalog_source,
    source_version,
    discovered_at,
    last_seen_at
)
SELECT
    l.language_code,
    l.language_code,

    EXISTS (
        SELECT 1
        FROM public.tts_model_language_capabilities c
        WHERE c.language_code = l.language_code
          AND c.is_enabled = true
          AND c.is_approved = true
    ),

    false,

    l.is_enabled,
    l.display_name,
    l.native_name,

    jsonb_build_object(
        'scope', 'generic_language',
        'generated', true
    ),

    l.language_code,
    NULL,
    NULL,
    NULL,
    NULL,

    true,
    'global_language_seed',
    '2026-08',
    now(),
    now()

FROM public.tts_languages l
WHERE l.is_enabled = true

ON CONFLICT (locale) DO UPDATE
SET
    language_code =
        COALESCE(
            public.tts_locales.language_code,
            EXCLUDED.language_code
        ),

    translator_lang =
        COALESCE(
            public.tts_locales.translator_lang,
            EXCLUDED.translator_lang
        ),

    display_name =
        COALESCE(
            public.tts_locales.display_name,
            EXCLUDED.display_name
        ),

    native_name =
        COALESCE(
            public.tts_locales.native_name,
            EXCLUDED.native_name
        ),

    tts_supported =
        public.tts_locales.tts_supported
        OR EXCLUDED.tts_supported,

    last_seen_at = now();


-- ------------------------------------------------------------
-- 2. GLOBAL LANGUAGE-CODE ALIASES
-- ------------------------------------------------------------

INSERT INTO public.tts_locale_aliases (
    alias_key,
    locale,
    language_code,
    alias_type,
    is_enabled,
    priority,
    meta_json
)
SELECT
    lower(btrim(language_code)),
    NULL,
    lower(btrim(language_code)),
    'language_code',
    true,
    30,
    jsonb_build_object(
        'source', 'tts_languages',
        'generated', true
    )
FROM public.tts_languages
WHERE is_enabled = true
  AND btrim(language_code) <> ''

ON CONFLICT (alias_key) DO UPDATE
SET
    is_enabled = true,
    meta_json =
        public.tts_locale_aliases.meta_json
        || EXCLUDED.meta_json;


-- ------------------------------------------------------------
-- 3. GLOBAL DISPLAY-NAME ALIASES
--
-- Example:
-- French  -> fr
-- Japanese -> ja
-- Swahili -> sw
--
-- Again: DB data, not Python constants.
-- ------------------------------------------------------------

INSERT INTO public.tts_locale_aliases (
    alias_key,
    locale,
    language_code,
    alias_type,
    is_enabled,
    priority,
    meta_json
)
SELECT
    lower(btrim(display_name)),
    NULL,
    language_code,
    'display_name',
    true,
    40,
    jsonb_build_object(
        'source', 'tts_languages.display_name',
        'generated', true
    )
FROM public.tts_languages
WHERE display_name IS NOT NULL
  AND btrim(display_name) <> ''
  AND is_enabled = true

ON CONFLICT (alias_key) DO NOTHING;


-- ------------------------------------------------------------
-- 4. NATIVE-NAME ALIASES WHEN AVAILABLE
-- ------------------------------------------------------------

INSERT INTO public.tts_locale_aliases (
    alias_key,
    locale,
    language_code,
    alias_type,
    is_enabled,
    priority,
    meta_json
)
SELECT
    lower(btrim(native_name)),
    NULL,
    language_code,
    'native_name',
    true,
    50,
    jsonb_build_object(
        'source', 'tts_languages.native_name',
        'generated', true
    )
FROM public.tts_languages
WHERE native_name IS NOT NULL
  AND btrim(native_name) <> ''
  AND is_enabled = true

ON CONFLICT (alias_key) DO NOTHING;


-- ------------------------------------------------------------
-- 5. MAKE GLOBAL LANGUAGE ALIASES DETERMINISTIC
--
-- Once a generic BCP-47 language locale exists (for example "en",
-- "hi", "fr"), language-name aliases should point directly to
-- that canonical generic locale.
--
-- This prevents:
--
--     English -> language_code=en
--
-- from becoming ambiguous when both "en" and "en-IN"/"en-US" etc.
-- exist.
--
-- Explicit regional input still resolves directly:
--
--     en-IN -> en-IN
--     hi-IN -> hi-IN
--
-- No geography decision is performed here.
-- ------------------------------------------------------------

UPDATE public.tts_locale_aliases a
SET
    locale = a.language_code,
    language_code = NULL,
    meta_json =
        a.meta_json
        || jsonb_build_object(
            'canonical_generic_locale', true
        )
WHERE a.alias_type IN (
        'language_code',
        'display_name',
        'native_name'
      )
  AND a.language_code IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM public.tts_locales l
      WHERE l.locale = a.language_code
        AND l.region_code IS NULL
        AND l.is_enabled = true
  );


-- ------------------------------------------------------------
-- 6. PRESERVE PRE-EIP7 LEGACY INDIA INPUT SEMANTICS
--
-- Existing clients historically using "in" or "india" meant
-- Hindi (India), not generic Hindi.
--
-- This compatibility relationship belongs in SQL masterdata,
-- never Python source.
-- ------------------------------------------------------------

UPDATE public.tts_locale_aliases
SET
    locale = 'hi-IN',
    language_code = NULL,
    meta_json =
        meta_json
        || jsonb_build_object(
            'canonicalized_by', 'eip7_global_audio',
            'legacy_target', 'hi-IN'
        )
WHERE alias_key IN ('in', 'india')
  AND alias_type = 'legacy_compat'
  AND EXISTS (
      SELECT 1
      FROM public.tts_locales
      WHERE locale = 'hi-IN'
  );


-- ------------------------------------------------------------
-- 7. PROVIDER + VOICE IDENTITY CONSTRAINT
--
-- Allows DB to guarantee that voice/model capability references
-- cannot accidentally cross providers.
-- ------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_tts_voices_provider_id'
    ) THEN
        ALTER TABLE public.tts_voices
        ADD CONSTRAINT uq_tts_voices_provider_id
        UNIQUE (provider, id);
    END IF;
END;
$$;


-- ------------------------------------------------------------
-- 6. VOICE <-> MODEL CAPABILITY
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_voice_model_capabilities (
    provider_code       text NOT NULL,
    voice_id            uuid NOT NULL,
    model_code          text NOT NULL,

    is_enabled          boolean NOT NULL DEFAULT true,
    is_approved         boolean NOT NULL DEFAULT true,

    supports_styles     boolean NULL,
    supports_emotions   boolean NULL,
    supports_streaming  boolean NULL,

    source              text NOT NULL DEFAULT 'catalog',
    source_version      text NULL,
    discovered_at       timestamptz NULL,
    last_seen_at        timestamptz NULL,

    meta_json           jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        provider_code,
        voice_id,
        model_code
    ),

    CONSTRAINT fk_tts_voice_model_voice
      FOREIGN KEY (
          provider_code,
          voice_id
      )
      REFERENCES public.tts_voices(
          provider,
          id
      )
      ON DELETE CASCADE,

    CONSTRAINT fk_tts_voice_model_model
      FOREIGN KEY (
          provider_code,
          model_code
      )
      REFERENCES public.tts_provider_models(
          provider_code,
          model_code
      )
      ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_tts_voice_model_lookup
    ON public.tts_voice_model_capabilities(
        provider_code,
        model_code,
        is_enabled,
        is_approved
    );


DROP TRIGGER IF EXISTS trg_tts_voice_model_set_updated_at
    ON public.tts_voice_model_capabilities;

CREATE TRIGGER trg_tts_voice_model_set_updated_at
BEFORE UPDATE ON public.tts_voice_model_capabilities
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- ------------------------------------------------------------
-- 7. CURRENT AZURE VOICES -> CURRENT AZURE MODEL
--
-- Existing runtime state is preserved.
-- ------------------------------------------------------------

INSERT INTO public.tts_voice_model_capabilities (
    provider_code,
    voice_id,
    model_code,
    is_enabled,
    is_approved,
    supports_styles,
    source,
    source_version,
    discovered_at,
    last_seen_at,
    meta_json
)
SELECT
    'azure',
    v.id,
    'speech_standard_neural',
    true,
    true,
    v.supports_styles,
    'existing_tts_voice_catalog',
    '2026-08',
    v.created_at,
    now(),
    '{"generated":true}'::jsonb

FROM public.tts_voices v
WHERE v.provider = 'azure'

ON CONFLICT (
    provider_code,
    voice_id,
    model_code
)
DO UPDATE SET
    is_enabled = true,
    supports_styles = EXCLUDED.supports_styles,
    last_seen_at = now();


COMMIT;
