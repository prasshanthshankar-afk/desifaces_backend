-- DEV-ONLY additive routing metadata. Apply manually only on desifaces-dev.
-- Use psql -X -v ON_ERROR_STOP=1 -1 -f <path>; transaction is atomic.
-- Preconditions: verify Azure VM identity, current database and no previous route.
DO $guard$
BEGIN
 IF current_database() <> 'desifaces'
    OR current_setting('application_name', true) <> 'desifaces_dev_translation_certification'
 THEN
   RAISE EXCEPTION 'DEV translation routing guard failed';
 END IF;
 IF (SELECT COUNT(*) FROM public.tts_locales
     WHERE locale IN ('en-IN','hi-IN') AND is_enabled) <> 2
 THEN
   RAISE EXCEPTION 'Both English and Hindi enabled locales required';
 END IF;
 IF EXISTS (SELECT 1 FROM public.tts_locales
            WHERE locale='hi-IN' AND COALESCE(meta_json::jsonb,'{}'::jsonb)
                  ? 'translation_routing_v1')
 THEN
   RAISE EXCEPTION 'Refusing to overwrite existing translation routing';
 END IF;
END $guard$;

UPDATE public.tts_locales
SET meta_json = jsonb_set(
    COALESCE(meta_json::jsonb,'{}'::jsonb),
    '{translation_routing_v1}',
    jsonb_build_object(
      'enabled', true,
      'capabilities', jsonb_build_array(jsonb_build_object(
        'source_locale','en-IN','target_locale','hi-IN',
        'provider','sarvam','model','mayura:v1',
        'provider_source_code','en-IN','provider_target_code','hi-IN',
        'approved',true,'enabled',true
      ))
    ), true
)
WHERE locale='hi-IN' AND is_enabled
  AND NOT (COALESCE(meta_json::jsonb,'{}'::jsonb) ? 'translation_routing_v1');

DO $verify$
BEGIN
 IF (SELECT COUNT(*) FROM public.tts_locales
     WHERE locale='hi-IN'
       AND meta_json::jsonb #>> '{translation_routing_v1,capabilities,0,provider}') <> 1
 THEN
   RAISE EXCEPTION 'Routing metadata postcondition failed';
 END IF;
END $verify$;
-- Rollback, DEV only, only when no other consumer changed the route:
-- UPDATE public.tts_locales SET meta_json = meta_json::jsonb - 'translation_routing_v1'
-- WHERE locale='hi-IN' AND meta_json::jsonb #>> '{translation_routing_v1,capabilities,0,provider}'='sarvam';
