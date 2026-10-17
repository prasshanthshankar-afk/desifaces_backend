-- DEV-only read-only schema assessment. No migration or pricing writes.
BEGIN READ ONLY;
SELECT current_database() AS db_name,
       current_setting('transaction_read_only') AS read_only;
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema='public'
 AND table_name IN ('tts_locales','tts_model_locale_capabilities',
 'tts_model_language_capabilities','tts_routing_policies',
 'tts_providers','audio_translation_capabilities')
ORDER BY table_name, ordinal_position;
SELECT to_regclass('public.audio_translation_capabilities') AS proposed_table_present;
SELECT policy_code, is_enabled, is_default, allow_provider_fallback,
       jsonb_object_keys(COALESCE(meta_json,'{}'::jsonb)) AS metadata_key
FROM public.tts_routing_policies
ORDER BY policy_code,metadata_key;
SELECT locale,language_code,translator_lang,translate_supported,
       jsonb_object_keys(COALESCE(meta_json,'{}'::jsonb)) AS metadata_key
FROM public.tts_locales
WHERE locale IN ('en-US','en-IN','hi-IN','ta-IN','or-IN','es-ES','ar-SA')
ORDER BY locale,metadata_key;
ROLLBACK;
