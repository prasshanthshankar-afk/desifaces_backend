-- ============================================================
-- #eip7
-- DB-backed locale resolution foundation.
--
-- Architecture rules:
--   * no country/language/locale mappings in application source
--   * aliases resolve through masterdata
--   * tts_locales remains the canonical locale catalog
--   * provider selection is intentionally OUT OF SCOPE here
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.tts_locale_aliases (
    alias_key       text PRIMARY KEY,
    locale          text NULL,
    language_code   text NULL,
    alias_type      text NOT NULL DEFAULT 'alias',
    is_enabled      boolean NOT NULL DEFAULT true,
    priority        integer NOT NULL DEFAULT 100,
    meta_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fk_tts_locale_aliases_locale
      FOREIGN KEY (locale)
      REFERENCES public.tts_locales(locale)
      ON DELETE RESTRICT,

    CONSTRAINT ck_tts_locale_aliases_target
      CHECK (
        (locale IS NOT NULL AND language_code IS NULL)
        OR
        (locale IS NULL AND language_code IS NOT NULL)
      ),

    CONSTRAINT ck_tts_locale_aliases_alias_key
      CHECK (length(btrim(alias_key)) > 0),

    CONSTRAINT ck_tts_locale_aliases_language_code
      CHECK (
        language_code IS NULL
        OR length(btrim(language_code)) > 0
      )
);

CREATE INDEX IF NOT EXISTS idx_tts_locale_aliases_locale
    ON public.tts_locale_aliases(locale)
    WHERE is_enabled = true;

CREATE INDEX IF NOT EXISTS idx_tts_locale_aliases_language
    ON public.tts_locale_aliases(language_code)
    WHERE is_enabled = true;

CREATE INDEX IF NOT EXISTS idx_tts_locale_aliases_type
    ON public.tts_locale_aliases(alias_type, is_enabled);

DROP TRIGGER IF EXISTS trg_tts_locale_aliases_set_updated_at
    ON public.tts_locale_aliases;

CREATE TRIGGER trg_tts_locale_aliases_set_updated_at
BEFORE UPDATE ON public.tts_locale_aliases
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();

-- ------------------------------------------------------------
-- Canonical BCP-47 locale aliases.
--
-- Example:
--   hi-IN -> alias_key hi-in -> locale hi-IN
--
-- Generated entirely from canonical masterdata.
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
    lower(replace(btrim(l.locale), '_', '-')),
    l.locale,
    NULL,
    'canonical_locale',
    true,
    10,
    jsonb_build_object(
        'source', 'tts_locales',
        'generated', true
    )
FROM public.tts_locales l
WHERE btrim(l.locale) <> ''
ON CONFLICT (alias_key) DO UPDATE
SET
    locale = EXCLUDED.locale,
    language_code = NULL,
    alias_type = EXCLUDED.alias_type,
    is_enabled = EXCLUDED.is_enabled,
    priority = EXCLUDED.priority,
    meta_json = public.tts_locale_aliases.meta_json
                || EXCLUDED.meta_json;

-- ------------------------------------------------------------
-- Language-code aliases.
--
-- These intentionally resolve to a LANGUAGE rather than directly
-- assuming a geographic locale.
--
-- LocaleResolver may resolve a language automatically only when
-- exactly one enabled canonical locale currently represents it.
-- If several locales exist, resolution is intentionally ambiguous
-- until DB policy/masterdata selects one.
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
SELECT DISTINCT
    lower(btrim(l.translator_lang)),
    NULL,
    lower(btrim(l.translator_lang)),
    'language_code',
    true,
    30,
    jsonb_build_object(
        'source', 'tts_locales.translator_lang',
        'generated', true
    )
FROM public.tts_locales l
WHERE l.translator_lang IS NOT NULL
  AND btrim(l.translator_lang) <> ''
ON CONFLICT (alias_key) DO NOTHING;

-- ------------------------------------------------------------
-- Friendly language-name aliases derived from DB display names.
--
-- "Hindi (India)" -> "hindi"
-- "Tamil (India)" -> "tamil"
--
-- No language names are embedded in application code.
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
SELECT DISTINCT
    lower(
        btrim(
            split_part(l.display_name, '(', 1)
        )
    ),
    NULL,
    lower(btrim(l.translator_lang)),
    'display_name',
    true,
    40,
    jsonb_build_object(
        'source', 'tts_locales.display_name',
        'generated', true
    )
FROM public.tts_locales l
WHERE l.display_name IS NOT NULL
  AND btrim(l.display_name) <> ''
  AND l.translator_lang IS NOT NULL
  AND btrim(l.translator_lang) <> ''
  AND btrim(split_part(l.display_name, '(', 1)) <> ''
ON CONFLICT (alias_key) DO NOTHING;

-- ------------------------------------------------------------
-- Native-name aliases derived from DB masterdata.
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
SELECT DISTINCT
    lower(btrim(l.native_name)),
    NULL,
    lower(btrim(l.translator_lang)),
    'native_name',
    true,
    50,
    jsonb_build_object(
        'source', 'tts_locales.native_name',
        'generated', true
    )
FROM public.tts_locales l
WHERE l.native_name IS NOT NULL
  AND btrim(l.native_name) <> ''
  AND l.translator_lang IS NOT NULL
  AND btrim(l.translator_lang) <> ''
ON CONFLICT (alias_key) DO NOTHING;

-- ------------------------------------------------------------
-- Legacy compatibility aliases.
--
-- These are deliberately VERSIONED DATA rather than Python rules.
-- They preserve inputs recognized by the current svc-audio code.
-- Future deprecation is therefore a data/config decision.
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
VALUES
    (
        'in',
        NULL,
        'hi',
        'legacy_compat',
        true,
        200,
        '{"reason":"pre-eip7 svc-audio compatibility"}'::jsonb
    ),
    (
        'india',
        NULL,
        'hi',
        'legacy_compat',
        true,
        200,
        '{"reason":"pre-eip7 svc-audio compatibility"}'::jsonb
    ),
    (
        'hindi-india',
        'hi-IN',
        NULL,
        'legacy_compat',
        true,
        200,
        '{"reason":"pre-eip7 svc-audio compatibility"}'::jsonb
    ),
    (
        'english-india',
        'en-IN',
        NULL,
        'legacy_compat',
        true,
        200,
        '{"reason":"pre-eip7 svc-audio compatibility"}'::jsonb
    )
ON CONFLICT (alias_key) DO NOTHING;

COMMIT;
