-- DEV first. Requires a backed-up active routing policy before application.
-- Changes automatic selection only; never rewrites existing voices or assets.
BEGIN;
DO $$
BEGIN
  IF (SELECT count(*) FROM public.tts_routing_policies WHERE is_enabled AND is_default) <> 1 THEN
    RAISE EXCEPTION 'exactly one active default TTS policy required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.tts_providers WHERE provider_code='sarvam' AND is_enabled AND routing_enabled) THEN
    RAISE EXCEPTION 'Sarvam must already be certified and routing enabled';
  END IF;
  IF (SELECT count(DISTINCT v.gender)
      FROM public.tts_voices v
      JOIN public.tts_voice_locale_capabilities vl ON vl.voice_id=v.id
      JOIN public.tts_voice_model_capabilities vm ON vm.voice_id=v.id AND vm.provider_code=v.provider
      JOIN public.tts_provider_models m ON m.provider_code=vm.provider_code AND m.model_code=vm.model_code
      WHERE v.provider='sarvam' AND v.gender IN ('male','female') AND vl.locale='hi-IN'
        AND vl.is_enabled AND vl.is_approved AND vm.is_enabled AND vm.is_approved
        AND m.is_enabled AND m.routing_enabled) <> 2 THEN
    RAISE EXCEPTION 'Hindi male and female Sarvam voices must both be executable';
  END IF;
END $$;

WITH eligible_languages AS (
  SELECT DISTINCT l.language_code
  FROM public.tts_model_locale_capabilities mlc
  JOIN public.tts_provider_models m USING(provider_code,model_code)
  JOIN public.tts_locales l ON l.locale=mlc.locale
  WHERE mlc.provider_code='sarvam' AND mlc.is_enabled AND mlc.is_approved
    AND m.is_enabled AND m.routing_enabled AND l.is_enabled
    AND l.language_code <> 'en'
  UNION
  SELECT DISTINCT mlng.language_code
  FROM public.tts_model_language_capabilities mlng
  JOIN public.tts_provider_models m USING(provider_code,model_code)
  WHERE mlng.provider_code='sarvam' AND mlng.is_enabled AND mlng.is_approved
    AND m.is_enabled AND m.routing_enabled AND mlng.language_code <> 'en'
), preferences AS (
  SELECT jsonb_object_agg(language_code,'sarvam'::text) AS value FROM eligible_languages
)
UPDATE public.tts_routing_policies rp
SET meta_json=coalesce(rp.meta_json,'{}'::jsonb) || jsonb_build_object(
  'preferred_provider_by_language',
  coalesce(rp.meta_json->'preferred_provider_by_language','{}'::jsonb) || coalesce(p.value,'{}'::jsonb)
)
FROM preferences p WHERE rp.is_enabled AND rp.is_default;

UPDATE public.masterdata_revision SET revision=revision+1 WHERE domain='tts';
COMMIT;
