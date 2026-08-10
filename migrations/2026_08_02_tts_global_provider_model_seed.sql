-- ============================================================
-- #eip7
-- Global TTS provider/model/language masterdata.
--
-- Provider/model facts are versioned data, never Python mappings.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. GLOBAL LANGUAGE CAPABILITY SEED
--
-- Initial worldwide language baseline is the current Eleven v3
-- published TTS language surface plus languages already present in
-- tts_locales. Provider catalog synchronization can extend this
-- without application-source changes.
-- ------------------------------------------------------------

INSERT INTO public.tts_languages (
    language_code,
    iso639_3,
    display_name,
    source,
    source_version
)
VALUES
('af','afr','Afrikaans','elevenlabs','2026-08'),
('ar','ara','Arabic','elevenlabs','2026-08'),
('hy','hye','Armenian','elevenlabs','2026-08'),
('as','asm','Assamese','elevenlabs','2026-08'),
('az','aze','Azerbaijani','elevenlabs','2026-08'),
('be','bel','Belarusian','elevenlabs','2026-08'),
('bn','ben','Bengali','elevenlabs','2026-08'),
('bs','bos','Bosnian','elevenlabs','2026-08'),
('bg','bul','Bulgarian','elevenlabs','2026-08'),
('ca','cat','Catalan','elevenlabs','2026-08'),
('ceb','ceb','Cebuano','elevenlabs','2026-08'),
('ny','nya','Chichewa','elevenlabs','2026-08'),
('hr','hrv','Croatian','elevenlabs','2026-08'),
('cs','ces','Czech','elevenlabs','2026-08'),
('da','dan','Danish','elevenlabs','2026-08'),
('nl','nld','Dutch','elevenlabs','2026-08'),
('en','eng','English','elevenlabs','2026-08'),
('et','est','Estonian','elevenlabs','2026-08'),
('fil','fil','Filipino','elevenlabs','2026-08'),
('fi','fin','Finnish','elevenlabs','2026-08'),
('fr','fra','French','elevenlabs','2026-08'),
('gl','glg','Galician','elevenlabs','2026-08'),
('ka','kat','Georgian','elevenlabs','2026-08'),
('de','deu','German','elevenlabs','2026-08'),
('el','ell','Greek','elevenlabs','2026-08'),
('gu','guj','Gujarati','elevenlabs','2026-08'),
('ha','hau','Hausa','elevenlabs','2026-08'),
('he','heb','Hebrew','elevenlabs','2026-08'),
('hi','hin','Hindi','elevenlabs','2026-08'),
('hu','hun','Hungarian','elevenlabs','2026-08'),
('is','isl','Icelandic','elevenlabs','2026-08'),
('id','ind','Indonesian','elevenlabs','2026-08'),
('ga','gle','Irish','elevenlabs','2026-08'),
('it','ita','Italian','elevenlabs','2026-08'),
('ja','jpn','Japanese','elevenlabs','2026-08'),
('jv','jav','Javanese','elevenlabs','2026-08'),
('kn','kan','Kannada','elevenlabs','2026-08'),
('kk','kaz','Kazakh','elevenlabs','2026-08'),
('ky','kir','Kyrgyz','elevenlabs','2026-08'),
('ko','kor','Korean','elevenlabs','2026-08'),
('lv','lav','Latvian','elevenlabs','2026-08'),
('ln','lin','Lingala','elevenlabs','2026-08'),
('lt','lit','Lithuanian','elevenlabs','2026-08'),
('lb','ltz','Luxembourgish','elevenlabs','2026-08'),
('mk','mkd','Macedonian','elevenlabs','2026-08'),
('ms','msa','Malay','elevenlabs','2026-08'),
('ml','mal','Malayalam','elevenlabs','2026-08'),
('zh','cmn','Mandarin Chinese','elevenlabs','2026-08'),
('mr','mar','Marathi','elevenlabs','2026-08'),
('ne','nep','Nepali','elevenlabs','2026-08'),
('no','nor','Norwegian','elevenlabs','2026-08'),
('ps','pus','Pashto','elevenlabs','2026-08'),
('fa','fas','Persian','elevenlabs','2026-08'),
('pl','pol','Polish','elevenlabs','2026-08'),
('pt','por','Portuguese','elevenlabs','2026-08'),
('pa','pan','Punjabi','elevenlabs','2026-08'),
('ro','ron','Romanian','elevenlabs','2026-08'),
('ru','rus','Russian','elevenlabs','2026-08'),
('sr','srp','Serbian','elevenlabs','2026-08'),
('sd','snd','Sindhi','elevenlabs','2026-08'),
('sk','slk','Slovak','elevenlabs','2026-08'),
('sl','slv','Slovenian','elevenlabs','2026-08'),
('so','som','Somali','elevenlabs','2026-08'),
('es','spa','Spanish','elevenlabs','2026-08'),
('sw','swa','Swahili','elevenlabs','2026-08'),
('sv','swe','Swedish','elevenlabs','2026-08'),
('ta','tam','Tamil','elevenlabs','2026-08'),
('te','tel','Telugu','elevenlabs','2026-08'),
('th','tha','Thai','elevenlabs','2026-08'),
('tr','tur','Turkish','elevenlabs','2026-08'),
('uk','ukr','Ukrainian','elevenlabs','2026-08'),
('ur','urd','Urdu','elevenlabs','2026-08'),
('vi','vie','Vietnamese','elevenlabs','2026-08'),
('cy','cym','Welsh','elevenlabs','2026-08'),

-- Provider/current-catalog language not included in the v3 list above.
('or','ori','Odia','svc-audio','2026-08')

ON CONFLICT (language_code) DO UPDATE
SET
    iso639_3 = COALESCE(
        public.tts_languages.iso639_3,
        EXCLUDED.iso639_3
    ),
    display_name = COALESCE(
        public.tts_languages.display_name,
        EXCLUDED.display_name
    );


-- ------------------------------------------------------------
-- 2. PROVIDERS
-- ------------------------------------------------------------

INSERT INTO public.tts_providers (
    provider_code,
    display_name,
    adapter_key,
    provider_type,
    is_enabled,
    routing_enabled,
    supports_catalog_sync,
    config_json,
    meta_json
)
VALUES
(
    'azure',
    'Azure Speech',
    'azure',
    'tts',
    true,
    true,
    true,
    '{"credential_prefix":"AZURE_SPEECH"}'::jsonb,
    '{"role":"global"}'::jsonb
),
(
    'elevenlabs',
    'ElevenLabs',
    'elevenlabs',
    'tts',
    true,
    false,
    true,
    '{"credential_prefix":"ELEVENLABS"}'::jsonb,
    '{"role":"global"}'::jsonb
),
(
    'sarvam',
    'Sarvam',
    'sarvam',
    'tts',
    true,
    false,
    true,
    '{"credential_prefix":"SARVAM"}'::jsonb,
    '{"role":"specialist"}'::jsonb
)
ON CONFLICT (provider_code) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    adapter_key = EXCLUDED.adapter_key,
    provider_type = EXCLUDED.provider_type,
    is_enabled = EXCLUDED.is_enabled,
    supports_catalog_sync = EXCLUDED.supports_catalog_sync,
    config_json = EXCLUDED.config_json,
    meta_json = EXCLUDED.meta_json;


-- ------------------------------------------------------------
-- 3. PROVIDER MODELS
--
-- routing_enabled remains false for new adapters until their
-- synthesis implementations and regression tests are validated.
-- ------------------------------------------------------------

INSERT INTO public.tts_provider_models (
    provider_code,
    model_code,
    provider_model_id,
    display_name,
    service_mode,
    max_input_chars,
    max_sample_rate_hz,
    supports_streaming,
    supports_multilingual,
    supports_ssml,
    supports_styles,
    supports_emotions,
    supports_pace,
    output_formats_json,
    quality_class,
    is_enabled,
    routing_enabled,
    source,
    source_version,
    meta_json
)
VALUES
(
    'azure',
    'speech_standard_neural',
    NULL,
    'Azure Speech Standard Neural',
    'tts',
    NULL,
    48000,
    true,
    true,
    true,
    true,
    true,
    true,
    '["mp3","wav"]'::jsonb,
    'high',
    true,
    true,
    'provider_catalog',
    '2026-08',
    '{"voice_catalog_driven":true}'::jsonb
),
(
    'elevenlabs',
    'eleven_v3',
    'eleven_v3',
    'Eleven v3',
    'tts',
    5000,
    NULL,
    true,
    true,
    false,
    true,
    true,
    false,
    '[]'::jsonb,
    'premium_expressive',
    true,
    false,
    'official_docs',
    '2026-08',
    '{"language_count":74}'::jsonb
),
(
    'elevenlabs',
    'eleven_multilingual_v2',
    'eleven_multilingual_v2',
    'Eleven Multilingual v2',
    'tts',
    10000,
    NULL,
    true,
    true,
    false,
    true,
    true,
    false,
    '[]'::jsonb,
    'premium_stable',
    true,
    false,
    'official_docs',
    '2026-08',
    '{"language_count":29,"longform":true}'::jsonb
),
(
    'elevenlabs',
    'eleven_flash_v2_5',
    'eleven_flash_v2_5',
    'Eleven Flash v2.5',
    'tts',
    40000,
    NULL,
    true,
    true,
    false,
    false,
    false,
    false,
    '[]'::jsonb,
    'low_latency',
    true,
    false,
    'official_docs',
    '2026-08',
    '{"language_count":32}'::jsonb
),
(
    'sarvam',
    'bulbul_v3',
    'bulbul:v3',
    'Sarvam Bulbul v3',
    'tts',
    2500,
    48000,
    true,
    true,
    false,
    false,
    true,
    true,
    '["wav","mp3","linear16","mulaw","alaw","opus","flac","aac"]'::jsonb,
    'specialist_high',
    true,
    false,
    'official_docs',
    '2026-08',
    '{"language_count":11,"code_mixed":true}'::jsonb
)
ON CONFLICT (provider_code, model_code) DO UPDATE
SET
    provider_model_id = EXCLUDED.provider_model_id,
    display_name = EXCLUDED.display_name,
    max_input_chars = EXCLUDED.max_input_chars,
    max_sample_rate_hz = EXCLUDED.max_sample_rate_hz,
    supports_streaming = EXCLUDED.supports_streaming,
    supports_multilingual = EXCLUDED.supports_multilingual,
    supports_ssml = EXCLUDED.supports_ssml,
    supports_styles = EXCLUDED.supports_styles,
    supports_emotions = EXCLUDED.supports_emotions,
    supports_pace = EXCLUDED.supports_pace,
    output_formats_json = EXCLUDED.output_formats_json,
    quality_class = EXCLUDED.quality_class,
    is_enabled = EXCLUDED.is_enabled,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version,
    meta_json = EXCLUDED.meta_json;


-- ------------------------------------------------------------
-- 4. ELEVEN V3 — 74 LANGUAGE CAPABILITIES
-- ------------------------------------------------------------

INSERT INTO public.tts_model_language_capabilities (
    provider_code,
    model_code,
    language_code,
    provider_language_code,
    source,
    source_version
)
SELECT
    'elevenlabs',
    'eleven_v3',
    l.language_code,
    l.iso639_3,
    'official_docs',
    '2026-08'
FROM public.tts_languages l
WHERE l.iso639_3 = ANY (
    ARRAY[
        'afr','ara','hye','asm','aze','bel','ben','bos','bul',
        'cat','ceb','nya','hrv','ces','dan','nld','eng','est',
        'fil','fin','fra','glg','kat','deu','ell','guj','hau',
        'heb','hin','hun','isl','ind','gle','ita','jpn','jav',
        'kan','kaz','kir','kor','lav','lin','lit','ltz','mkd',
        'msa','mal','cmn','mar','nep','nor','pus','fas','pol',
        'por','pan','ron','rus','srp','snd','slk','slv','som',
        'spa','swa','swe','tam','tel','tha','tur','ukr','urd',
        'vie','cym'
    ]::text[]
)
ON CONFLICT (provider_code, model_code, language_code)
DO UPDATE SET
    provider_language_code = EXCLUDED.provider_language_code,
    is_enabled = true,
    is_approved = true,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version;


-- ------------------------------------------------------------
-- 5. ELEVEN MULTILINGUAL V2 — 29 LANGUAGES
-- ------------------------------------------------------------

INSERT INTO public.tts_model_language_capabilities (
    provider_code,
    model_code,
    language_code,
    provider_language_code,
    source,
    source_version
)
SELECT
    'elevenlabs',
    'eleven_multilingual_v2',
    l.language_code,
    l.iso639_3,
    'official_docs',
    '2026-08'
FROM public.tts_languages l
WHERE l.iso639_3 = ANY (
    ARRAY[
        'ara','bul','cmn','hrv','ces','dan','nld','eng','fil',
        'fin','fra','deu','ell','hin','ind','ita','jpn','kor',
        'msa','pol','por','ron','rus','slk','spa','swe','tam',
        'tur','ukr'
    ]::text[]
)
ON CONFLICT (provider_code, model_code, language_code)
DO UPDATE SET
    provider_language_code = EXCLUDED.provider_language_code,
    is_enabled = true,
    is_approved = true,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version;


-- ------------------------------------------------------------
-- 6. ELEVEN FLASH V2.5 — 32 LANGUAGES
-- ------------------------------------------------------------

INSERT INTO public.tts_model_language_capabilities (
    provider_code,
    model_code,
    language_code,
    provider_language_code,
    source,
    source_version
)
SELECT
    'elevenlabs',
    'eleven_flash_v2_5',
    l.language_code,
    l.iso639_3,
    'official_docs',
    '2026-08'
FROM public.tts_languages l
WHERE l.iso639_3 = ANY (
    ARRAY[
        'ara','bul','cmn','hrv','ces','dan','nld','eng','fil',
        'fin','fra','deu','ell','hun','hin','ind','ita','jpn',
        'kor','msa','nor','pol','por','ron','rus','slk','spa',
        'swe','tam','tur','ukr','vie'
    ]::text[]
)
ON CONFLICT (provider_code, model_code, language_code)
DO UPDATE SET
    provider_language_code = EXCLUDED.provider_language_code,
    is_enabled = true,
    is_approved = true,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version;


-- ------------------------------------------------------------
-- 7. SARVAM BULBUL V3 LANGUAGE CAPABILITY
-- ------------------------------------------------------------

INSERT INTO public.tts_model_language_capabilities (
    provider_code,
    model_code,
    language_code,
    source,
    source_version
)
SELECT
    'sarvam',
    'bulbul_v3',
    language_code,
    'official_docs',
    '2026-08'
FROM public.tts_languages
WHERE language_code = ANY (
    ARRAY[
        'en','hi','bn','ta','te',
        'kn','ml','mr','gu','pa','or'
    ]::text[]
)
ON CONFLICT (provider_code, model_code, language_code)
DO UPDATE SET
    is_enabled = true,
    is_approved = true,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version;


-- ------------------------------------------------------------
-- 8. SARVAM EXACT PROVIDER LOCALE MAPPING
--
-- Canonical desifaces Odia remains or-IN.
-- Sarvam currently expects provider code od-IN.
-- This translation is DB data.
-- ------------------------------------------------------------

INSERT INTO public.tts_model_locale_capabilities (
    provider_code,
    model_code,
    locale,
    provider_locale_code,
    source,
    source_version
)
VALUES
('sarvam','bulbul_v3','en-IN','en-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','hi-IN','hi-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','bn-IN','bn-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','ta-IN','ta-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','te-IN','te-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','kn-IN','kn-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','ml-IN','ml-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','mr-IN','mr-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','gu-IN','gu-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','pa-IN','pa-IN','official_docs','2026-08'),
('sarvam','bulbul_v3','or-IN','od-IN','official_docs','2026-08')
ON CONFLICT (provider_code, model_code, locale)
DO UPDATE SET
    provider_locale_code = EXCLUDED.provider_locale_code,
    is_enabled = true,
    is_approved = true,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version;


-- ------------------------------------------------------------
-- 9. CURRENT AZURE MODEL-LOCALE CAPABILITY
--
-- Existing voice inventory is used as current evidence.
-- Future Azure catalog sync expands this automatically.
-- ------------------------------------------------------------

INSERT INTO public.tts_model_locale_capabilities (
    provider_code,
    model_code,
    locale,
    provider_locale_code,
    source,
    source_version
)
SELECT DISTINCT
    'azure',
    'speech_standard_neural',
    v.locale,
    v.locale,
    'existing_voice_catalog',
    '2026-08'
FROM public.tts_voices v
WHERE v.provider = 'azure'
  AND v.locale IS NOT NULL
ON CONFLICT (provider_code, model_code, locale)
DO UPDATE SET
    provider_locale_code = EXCLUDED.provider_locale_code,
    is_enabled = true,
    is_approved = true,
    source = EXCLUDED.source,
    source_version = EXCLUDED.source_version;


COMMIT;
