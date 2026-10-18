-- DEV ONLY. Execute only after independent six-turn en-US -> hi-IN regional and semantic QA passes.
-- Existing en-IN -> hi-IN route MUST remain untouched.
-- Requires psql application_name=desifaces_dev_translation_certification and an explicit
-- operator quality-approval marker; does not modify pricing or other locales.
DO $guard$
DECLARE routes jsonb; capabilities jsonb;
BEGIN
 IF current_database() <> 'desifaces'
 OR current_setting('application_name', true) <> 'desifaces_dev_translation_certification'
 OR current_setting('desifaces.dev_en_us_hi_in_quality_approved', true) <> 'yes'
 THEN RAISE EXCEPTION 'DEV locale approval/environment guard failed'; END IF;
 IF (SELECT count(*) FROM public.tts_locales
     WHERE locale IN ('en-US','en-IN','hi-IN') AND is_enabled) <> 3
 THEN RAISE EXCEPTION 'All three exact locales must be enabled'; END IF;
 SELECT meta_json::jsonb -> 'translation_routing_v1'
 INTO routes FROM public.tts_locales WHERE locale='hi-IN';
 IF routes IS NULL OR routes->>'enabled' <> 'true'
 OR jsonb_typeof(routes->'capabilities') <> 'array'
 THEN RAISE EXCEPTION 'Existing approved Hindi metadata required'; END IF;
 capabilities := routes->'capabilities';
 IF NOT EXISTS (
  SELECT 1 FROM jsonb_array_elements(capabilities) AS c
  WHERE c->>'source_locale'='en-IN'
    AND c->>'target_locale'='hi-IN'
    AND c->>'provider'='sarvam'
    AND c->>'model'='mayura:v1'
    AND c->>'approved'='true' AND c->>'enabled'='true')
 THEN RAISE EXCEPTION 'Original en-IN route must remain approved'; END IF;
 IF EXISTS (
  SELECT 1 FROM jsonb_array_elements(capabilities) AS c
  WHERE c->>'source_locale'='en-US'
    AND c->>'target_locale'='hi-IN')
 THEN RAISE EXCEPTION 'Refusing to overwrite existing en-US direction'; END IF;
END $guard$;

UPDATE public.tts_locales
SET meta_json = jsonb_set(
  meta_json::jsonb,
  '{translation_routing_v1,capabilities}',
  (meta_json::jsonb #> '{translation_routing_v1,capabilities}')
    || jsonb_build_array(jsonb_build_object(
         'source_locale','en-US','target_locale','hi-IN',
         'provider','sarvam','model','mayura:v1',
         'provider_source_code','en-IN','provider_target_code','hi-IN',
         'approved',true,'enabled',true)),
  false)
WHERE locale='hi-IN' AND is_enabled;

DO $verify$
BEGIN
 IF (SELECT count(*) FROM public.tts_locales l,
  jsonb_array_elements(l.meta_json::jsonb #> '{translation_routing_v1,capabilities}') c
  WHERE l.locale='hi-IN' AND c->>'target_locale'='hi-IN'
  AND c->>'source_locale' IN ('en-IN','en-US')
  AND c->>'approved'='true' AND c->>'enabled'='true') <> 2
 THEN RAISE EXCEPTION 'Expected both approved English-to-Hindi routes'; END IF;
END $verify$;
