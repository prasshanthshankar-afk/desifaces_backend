-- ============================================================
-- #eip7
-- Global svc-audio masterdata platform.
--
-- PRINCIPLES
-- ----------
-- 1. User supplies country/region/locale/accent/language/script.
-- 2. svc-audio resolves provider/model/voice.
-- 3. Geographic/language/provider knowledge lives in DB data.
-- 4. No country -> provider routing exists in application source.
-- 5. tts_locales remains canonical locale catalog.
-- 6. tts_voices owns provider-native voice identity.
-- 7. Multilingual voice capability is modeled independently.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. GLOBAL LANGUAGE MASTERDATA
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_languages (
    language_code       text PRIMARY KEY,
    iso639_3            text NULL,
    display_name        text NULL,
    native_name         text NULL,
    script_codes        text[] NOT NULL DEFAULT '{}'::text[],
    is_enabled          boolean NOT NULL DEFAULT true,
    source              text NOT NULL DEFAULT 'masterdata',
    source_version      text NULL,
    meta_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_tts_languages_code
      CHECK (length(btrim(language_code)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tts_languages_iso639_3
    ON public.tts_languages(lower(iso639_3))
    WHERE iso639_3 IS NOT NULL;

DROP TRIGGER IF EXISTS trg_tts_languages_set_updated_at
    ON public.tts_languages;

CREATE TRIGGER trg_tts_languages_set_updated_at
BEFORE UPDATE ON public.tts_languages
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- Preserve every language already represented by tts_locales.

INSERT INTO public.tts_languages (
    language_code,
    source,
    meta_json
)
SELECT DISTINCT
    lower(btrim(translator_lang)),
    'existing_tts_locales',
    '{"generated":true}'::jsonb
FROM public.tts_locales
WHERE translator_lang IS NOT NULL
  AND btrim(translator_lang) <> ''
ON CONFLICT (language_code) DO NOTHING;


-- ------------------------------------------------------------
-- 2. ENRICH EXISTING CANONICAL LOCALE TABLE
-- ------------------------------------------------------------

ALTER TABLE public.tts_locales
    ADD COLUMN IF NOT EXISTS language_code text NULL,
    ADD COLUMN IF NOT EXISTS region_code text NULL,
    ADD COLUMN IF NOT EXISTS country_code text NULL,
    ADD COLUMN IF NOT EXISTS script_code text NULL,
    ADD COLUMN IF NOT EXISTS accent_code text NULL,
    ADD COLUMN IF NOT EXISTS is_user_selectable boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS catalog_source text NOT NULL DEFAULT 'masterdata',
    ADD COLUMN IF NOT EXISTS source_version text NULL,
    ADD COLUMN IF NOT EXISTS discovered_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NULL;


-- Generic syntax extraction only.
-- No country/language business rules are embedded here.

UPDATE public.tts_locales
SET language_code =
        COALESCE(
            NULLIF(lower(btrim(language_code)), ''),
            NULLIF(lower(btrim(translator_lang)), ''),
            NULLIF(
                lower(split_part(replace(locale, '_', '-'), '-', 1)),
                ''
            )
        )
WHERE language_code IS NULL
   OR btrim(language_code) = '';


UPDATE public.tts_locales
SET region_code =
    CASE
        WHEN array_length(
            string_to_array(replace(locale, '_', '-'), '-'),
            1
        ) >= 2
        THEN upper(
            (
                string_to_array(
                    replace(locale, '_', '-'),
                    '-'
                )
            )[2]
        )
        ELSE NULL
    END
WHERE region_code IS NULL;


-- country_code is populated only when the BCP-47 region subtag
-- is a two-letter alphabetic region. Numeric/macro-regions remain
-- region_code only.

UPDATE public.tts_locales
SET country_code =
    CASE
        WHEN region_code ~ '^[A-Z]{2}$'
        THEN region_code
        ELSE NULL
    END
WHERE country_code IS NULL;


DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_tts_locales_language'
    ) THEN
        ALTER TABLE public.tts_locales
        ADD CONSTRAINT fk_tts_locales_language
        FOREIGN KEY (language_code)
        REFERENCES public.tts_languages(language_code)
        ON DELETE RESTRICT;
    END IF;
END;
$$;


CREATE INDEX IF NOT EXISTS idx_tts_locales_language
    ON public.tts_locales(language_code);

CREATE INDEX IF NOT EXISTS idx_tts_locales_region
    ON public.tts_locales(region_code);

CREATE INDEX IF NOT EXISTS idx_tts_locales_country
    ON public.tts_locales(country_code);

CREATE INDEX IF NOT EXISTS idx_tts_locales_accent
    ON public.tts_locales(accent_code);


-- Existing locale on tts_voices becomes a legacy/home locale.
-- Multilingual capability belongs in tts_voice_locale_capabilities.
--
-- Azure rows remain unchanged. New multilingual voices are allowed
-- to have no single home locale.

ALTER TABLE public.tts_voices
    ALTER COLUMN locale DROP NOT NULL;


-- ------------------------------------------------------------
-- 3. USER-CONTEXT / LOCALE RELATIONSHIP MASTERDATA
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_locale_context_rules (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    locale              text NOT NULL,
    context_type        text NOT NULL,
    context_value       text NOT NULL,
    match_weight        numeric(8,4) NOT NULL DEFAULT 1.0000,
    is_enabled          boolean NOT NULL DEFAULT true,
    meta_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fk_tts_locale_context_locale
      FOREIGN KEY (locale)
      REFERENCES public.tts_locales(locale)
      ON DELETE CASCADE,

    CONSTRAINT ck_tts_locale_context_type
      CHECK (
        context_type IN (
            'country',
            'region',
            'accent',
            'dialect'
        )
      ),

    CONSTRAINT ck_tts_locale_context_value
      CHECK (length(btrim(context_value)) > 0),

    CONSTRAINT uq_tts_locale_context_rule
      UNIQUE (locale, context_type, context_value)
);


CREATE INDEX IF NOT EXISTS idx_tts_locale_context_lookup
    ON public.tts_locale_context_rules(
        context_type,
        context_value,
        is_enabled
    );


DROP TRIGGER IF EXISTS trg_tts_locale_context_set_updated_at
    ON public.tts_locale_context_rules;

CREATE TRIGGER trg_tts_locale_context_set_updated_at
BEFORE UPDATE ON public.tts_locale_context_rules
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- Region relationships derived from canonical BCP-47 masterdata.
-- This is generic syntax-derived data, not routing behavior.

INSERT INTO public.tts_locale_context_rules (
    locale,
    context_type,
    context_value,
    match_weight,
    meta_json
)
SELECT
    locale,
    'region',
    region_code,
    1.0000,
    '{"source":"tts_locales.region_code","generated":true}'::jsonb
FROM public.tts_locales
WHERE region_code IS NOT NULL
  AND btrim(region_code) <> ''
ON CONFLICT (locale, context_type, context_value)
DO NOTHING;


INSERT INTO public.tts_locale_context_rules (
    locale,
    context_type,
    context_value,
    match_weight,
    meta_json
)
SELECT
    locale,
    'country',
    country_code,
    1.0000,
    '{"source":"tts_locales.country_code","generated":true}'::jsonb
FROM public.tts_locales
WHERE country_code IS NOT NULL
  AND btrim(country_code) <> ''
ON CONFLICT (locale, context_type, context_value)
DO NOTHING;


-- ------------------------------------------------------------
-- 4. TTS PROVIDERS
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_providers (
    provider_code       text PRIMARY KEY,
    display_name        text NOT NULL,
    adapter_key         text NOT NULL,
    provider_type       text NOT NULL DEFAULT 'tts',
    is_enabled          boolean NOT NULL DEFAULT true,
    routing_enabled     boolean NOT NULL DEFAULT false,
    supports_catalog_sync boolean NOT NULL DEFAULT false,
    config_json         jsonb NOT NULL DEFAULT '{}'::jsonb,
    meta_json           jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_tts_provider_code
      CHECK (length(btrim(provider_code)) > 0)
);


DROP TRIGGER IF EXISTS trg_tts_providers_set_updated_at
    ON public.tts_providers;

CREATE TRIGGER trg_tts_providers_set_updated_at
BEFORE UPDATE ON public.tts_providers
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- ------------------------------------------------------------
-- 5. PROVIDER TTS MODELS
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_provider_models (
    provider_code       text NOT NULL,
    model_code          text NOT NULL,
    provider_model_id   text NULL,
    display_name        text NOT NULL,

    service_mode        text NOT NULL DEFAULT 'tts',

    max_input_chars     integer NULL,
    max_sample_rate_hz  integer NULL,

    supports_streaming  boolean NOT NULL DEFAULT false,
    supports_multilingual boolean NOT NULL DEFAULT false,
    supports_ssml       boolean NOT NULL DEFAULT false,
    supports_styles     boolean NOT NULL DEFAULT false,
    supports_emotions   boolean NOT NULL DEFAULT false,
    supports_pace       boolean NOT NULL DEFAULT false,

    output_formats_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    quality_class       text NULL,

    is_enabled          boolean NOT NULL DEFAULT true,
    routing_enabled     boolean NOT NULL DEFAULT false,

    source              text NOT NULL DEFAULT 'seed',
    source_version      text NULL,
    discovered_at       timestamptz NULL,
    last_seen_at        timestamptz NULL,

    meta_json           jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (provider_code, model_code),

    CONSTRAINT fk_tts_provider_models_provider
      FOREIGN KEY (provider_code)
      REFERENCES public.tts_providers(provider_code)
      ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_tts_models_routing
    ON public.tts_provider_models(
        routing_enabled,
        is_enabled,
        quality_class
    );


DROP TRIGGER IF EXISTS trg_tts_provider_models_set_updated_at
    ON public.tts_provider_models;

CREATE TRIGGER trg_tts_provider_models_set_updated_at
BEFORE UPDATE ON public.tts_provider_models
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- ------------------------------------------------------------
-- 6. MODEL -> LANGUAGE CAPABILITIES
--
-- Used by providers which advertise language-level capability
-- instead of strict locale-level capability.
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_model_language_capabilities (
    provider_code           text NOT NULL,
    model_code              text NOT NULL,
    language_code           text NOT NULL,
    provider_language_code  text NULL,

    support_level           text NOT NULL DEFAULT 'supported',
    is_enabled              boolean NOT NULL DEFAULT true,
    is_approved             boolean NOT NULL DEFAULT true,

    source                  text NOT NULL DEFAULT 'seed',
    source_version          text NULL,
    last_seen_at            timestamptz NULL,

    meta_json               jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        provider_code,
        model_code,
        language_code
    ),

    CONSTRAINT fk_tts_model_language_model
      FOREIGN KEY (provider_code, model_code)
      REFERENCES public.tts_provider_models(
          provider_code,
          model_code
      )
      ON DELETE CASCADE,

    CONSTRAINT fk_tts_model_language_language
      FOREIGN KEY (language_code)
      REFERENCES public.tts_languages(language_code)
      ON DELETE RESTRICT
);


CREATE INDEX IF NOT EXISTS idx_tts_model_language_lookup
    ON public.tts_model_language_capabilities(
        language_code,
        is_enabled,
        is_approved
    );


DROP TRIGGER IF EXISTS trg_tts_model_language_set_updated_at
    ON public.tts_model_language_capabilities;

CREATE TRIGGER trg_tts_model_language_set_updated_at
BEFORE UPDATE ON public.tts_model_language_capabilities
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- ------------------------------------------------------------
-- 7. MODEL -> LOCALE CAPABILITIES
--
-- provider_locale_code allows canonical desifaces locale to differ
-- from the code expected by a provider.
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_model_locale_capabilities (
    provider_code           text NOT NULL,
    model_code              text NOT NULL,
    locale                  text NOT NULL,
    provider_locale_code    text NULL,

    accent_code             text NULL,
    support_level           text NOT NULL DEFAULT 'supported',

    is_enabled              boolean NOT NULL DEFAULT true,
    is_approved             boolean NOT NULL DEFAULT true,

    source                  text NOT NULL DEFAULT 'seed',
    source_version          text NULL,
    last_seen_at            timestamptz NULL,

    meta_json               jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        provider_code,
        model_code,
        locale
    ),

    CONSTRAINT fk_tts_model_locale_model
      FOREIGN KEY (provider_code, model_code)
      REFERENCES public.tts_provider_models(
          provider_code,
          model_code
      )
      ON DELETE CASCADE,

    CONSTRAINT fk_tts_model_locale_locale
      FOREIGN KEY (locale)
      REFERENCES public.tts_locales(locale)
      ON DELETE RESTRICT
);


CREATE INDEX IF NOT EXISTS idx_tts_model_locale_lookup
    ON public.tts_model_locale_capabilities(
        locale,
        is_enabled,
        is_approved
    );


DROP TRIGGER IF EXISTS trg_tts_model_locale_set_updated_at
    ON public.tts_model_locale_capabilities;

CREATE TRIGGER trg_tts_model_locale_set_updated_at
BEFORE UPDATE ON public.tts_model_locale_capabilities
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- ------------------------------------------------------------
-- 8. MULTILINGUAL VOICE -> LOCALE CAPABILITY
--
-- tts_voices remains provider-native voice identity.
-- This table represents the actual locales/accents a voice can
-- synthesize well.
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_voice_locale_capabilities (
    voice_id                uuid NOT NULL,
    locale                  text NOT NULL,
    accent_code             text NOT NULL DEFAULT '',

    is_native_fit           boolean NOT NULL DEFAULT false,
    is_recommended          boolean NOT NULL DEFAULT false,
    is_enabled              boolean NOT NULL DEFAULT true,
    is_approved             boolean NOT NULL DEFAULT true,

    quality_score           numeric(8,4) NULL,

    source                  text NOT NULL DEFAULT 'catalog',
    source_version          text NULL,
    last_seen_at            timestamptz NULL,

    meta_json               jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        voice_id,
        locale,
        accent_code
    ),

    CONSTRAINT fk_tts_voice_locale_voice
      FOREIGN KEY (voice_id)
      REFERENCES public.tts_voices(id)
      ON DELETE CASCADE,

    CONSTRAINT fk_tts_voice_locale_locale
      FOREIGN KEY (locale)
      REFERENCES public.tts_locales(locale)
      ON DELETE RESTRICT
);


CREATE INDEX IF NOT EXISTS idx_tts_voice_locale_lookup
    ON public.tts_voice_locale_capabilities(
        locale,
        is_enabled,
        is_approved,
        quality_score DESC
    );


DROP TRIGGER IF EXISTS trg_tts_voice_locale_set_updated_at
    ON public.tts_voice_locale_capabilities;

CREATE TRIGGER trg_tts_voice_locale_set_updated_at
BEFORE UPDATE ON public.tts_voice_locale_capabilities
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- Existing Azure voices get their current home-locale capability.

INSERT INTO public.tts_voice_locale_capabilities (
    voice_id,
    locale,
    accent_code,
    is_native_fit,
    is_recommended,
    source,
    meta_json
)
SELECT
    id,
    locale,
    '',
    true,
    is_default,
    'existing_tts_voices',
    '{"generated":true}'::jsonb
FROM public.tts_voices
WHERE locale IS NOT NULL
ON CONFLICT (voice_id, locale, accent_code)
DO NOTHING;


-- ------------------------------------------------------------
-- 9. QUALITY BENCHMARK DATA
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_quality_profiles (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    provider_code           text NOT NULL,
    model_code              text NOT NULL,
    locale                  text NULL,
    voice_id                uuid NULL,
    accent_code             text NULL,

    naturalness_score       numeric(8,4) NULL,
    pronunciation_score     numeric(8,4) NULL,
    accent_fit_score        numeric(8,4) NULL,
    expression_score        numeric(8,4) NULL,
    gender_fit_score        numeric(8,4) NULL,
    longform_score          numeric(8,4) NULL,
    overall_score           numeric(8,4) NULL,

    sample_count            integer NOT NULL DEFAULT 0,
    benchmark_version       text NULL,
    is_approved             boolean NOT NULL DEFAULT false,
    measured_at             timestamptz NULL,

    meta_json               jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fk_tts_quality_model
      FOREIGN KEY (provider_code, model_code)
      REFERENCES public.tts_provider_models(
          provider_code,
          model_code
      )
      ON DELETE CASCADE,

    CONSTRAINT fk_tts_quality_locale
      FOREIGN KEY (locale)
      REFERENCES public.tts_locales(locale)
      ON DELETE CASCADE,

    CONSTRAINT fk_tts_quality_voice
      FOREIGN KEY (voice_id)
      REFERENCES public.tts_voices(id)
      ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_tts_quality_resolution
    ON public.tts_quality_profiles(
        provider_code,
        model_code,
        locale,
        is_approved,
        overall_score DESC
    );


DROP TRIGGER IF EXISTS trg_tts_quality_set_updated_at
    ON public.tts_quality_profiles;

CREATE TRIGGER trg_tts_quality_set_updated_at
BEFORE UPDATE ON public.tts_quality_profiles
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- ------------------------------------------------------------
-- 10. PROVIDER COST MASTERDATA
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_provider_cost_profiles (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    provider_code           text NOT NULL,
    model_code              text NOT NULL,

    unit_type               text NOT NULL,
    unit_size               numeric(18,6) NOT NULL DEFAULT 1,
    unit_cost               numeric(18,8) NOT NULL,
    currency                text NOT NULL DEFAULT 'USD',

    effective_from          timestamptz NOT NULL DEFAULT now(),
    effective_to            timestamptz NULL,

    is_enabled              boolean NOT NULL DEFAULT true,

    source                  text NOT NULL DEFAULT 'masterdata',
    meta_json               jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT fk_tts_cost_model
      FOREIGN KEY (provider_code, model_code)
      REFERENCES public.tts_provider_models(
          provider_code,
          model_code
      )
      ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_tts_cost_active
    ON public.tts_provider_cost_profiles(
        provider_code,
        model_code,
        is_enabled,
        effective_from DESC
    );


DROP TRIGGER IF EXISTS trg_tts_cost_set_updated_at
    ON public.tts_provider_cost_profiles;

CREATE TRIGGER trg_tts_cost_set_updated_at
BEFORE UPDATE ON public.tts_provider_cost_profiles
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


-- ------------------------------------------------------------
-- 11. ROUTING POLICY
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tts_routing_policies (
    policy_code             text PRIMARY KEY,
    display_name            text NOT NULL,
    description             text NULL,

    require_approved_capability boolean NOT NULL DEFAULT true,
    require_approved_quality boolean NOT NULL DEFAULT false,
    allow_provider_fallback boolean NOT NULL DEFAULT true,

    is_default              boolean NOT NULL DEFAULT false,
    is_enabled              boolean NOT NULL DEFAULT true,

    meta_json               jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);


CREATE UNIQUE INDEX IF NOT EXISTS uq_tts_routing_default
    ON public.tts_routing_policies(is_default)
    WHERE is_default = true
      AND is_enabled = true;


DROP TRIGGER IF EXISTS trg_tts_routing_policy_set_updated_at
    ON public.tts_routing_policies;

CREATE TRIGGER trg_tts_routing_policy_set_updated_at
BEFORE UPDATE ON public.tts_routing_policies
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


CREATE TABLE IF NOT EXISTS public.tts_routing_policy_weights (
    policy_code             text NOT NULL,
    dimension_code          text NOT NULL,
    weight                  numeric(8,4) NOT NULL,
    sort_order              integer NOT NULL DEFAULT 100,

    is_enabled              boolean NOT NULL DEFAULT true,
    meta_json               jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        policy_code,
        dimension_code
    ),

    CONSTRAINT fk_tts_routing_weight_policy
      FOREIGN KEY (policy_code)
      REFERENCES public.tts_routing_policies(policy_code)
      ON DELETE CASCADE,

    CONSTRAINT ck_tts_routing_weight
      CHECK (weight >= 0)
);


DROP TRIGGER IF EXISTS trg_tts_routing_weight_set_updated_at
    ON public.tts_routing_policy_weights;

CREATE TRIGGER trg_tts_routing_weight_set_updated_at
BEFORE UPDATE ON public.tts_routing_policy_weights
FOR EACH ROW
EXECUTE FUNCTION public.fn_set_updated_at();


COMMIT;
