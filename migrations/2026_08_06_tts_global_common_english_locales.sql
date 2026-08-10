BEGIN;

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
VALUES
(
    'en-US','en',true,true,true,
    'English (United States)','English',
    '{"scope":"global_common_english","generated":true}'::jsonb,
    'en','US','US',NULL,NULL,true,
    'global_language_seed','2026-08',now(),now()
),
(
    'en-GB','en',true,true,true,
    'English (United Kingdom)','English',
    '{"scope":"global_common_english","generated":true}'::jsonb,
    'en','GB','GB',NULL,NULL,true,
    'global_language_seed','2026-08',now(),now()
)
ON CONFLICT (locale)
DO UPDATE SET
    translator_lang='en',
    tts_supported=true,
    translate_supported=true,
    is_enabled=true,
    language_code='en',
    is_user_selectable=true,
    catalog_source='global_language_seed',
    source_version='2026-08',
    last_seen_at=now(),
    updated_at=now();

UPDATE public.masterdata_revision
SET revision=GREATEST(revision,15)
WHERE domain='tts';

COMMIT;
