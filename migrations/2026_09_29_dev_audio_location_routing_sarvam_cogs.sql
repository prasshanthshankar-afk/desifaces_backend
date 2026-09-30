-- desifaces DEV audio routing alignment — 2026-09-29
-- Scope:
--   * preserve DB-driven provider/model routing
--   * make location/locale capability the eligibility boundary
--   * keep Sarvam as a specialist for its supported Indian-region locales
--   * keep Azure/ElevenLabs globally routable
--   * install provider-cost masterdata
--   * make customer Audio SKU COGS conservative across currently routed providers
--
-- No synthesis adapter behavior, voice catalog contents, pricing credits,
-- subscription plans, or generation workflow are changed.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.tts_providers') IS NULL
     OR to_regclass('public.tts_provider_models') IS NULL
     OR to_regclass('public.tts_model_locale_capabilities') IS NULL
     OR to_regclass('public.tts_provider_cost_profiles') IS NULL
     OR to_regclass('public.pricing_sku_costs') IS NULL THEN
    RAISE EXCEPTION 'audio routing alignment requires existing global TTS masterdata + pricing cost tables';
  END IF;
END $$;

-- Provider roles are descriptive. Eligibility remains capability-driven.
UPDATE public.tts_providers
SET is_enabled=true,
    routing_enabled=true,
    meta_json=COALESCE(meta_json,'{}'::jsonb) || CASE provider_code
      WHEN 'sarvam' THEN jsonb_build_object(
        'role','specialist',
        'market_scope','south_asia_specialist',
        'eligibility','locale_capability_driven',
        'routing_contract','2026-09-29'
      )
      WHEN 'azure' THEN jsonb_build_object(
        'role','global',
        'market_scope','global',
        'eligibility','locale_capability_driven',
        'routing_contract','2026-09-29'
      )
      WHEN 'elevenlabs' THEN jsonb_build_object(
        'role','global',
        'market_scope','global',
        'eligibility','locale_capability_driven',
        'routing_contract','2026-09-29'
      )
      ELSE '{}'::jsonb
    END
WHERE provider_code IN ('azure','elevenlabs','sarvam');

-- Explicitly keep currently supported production models routable.
UPDATE public.tts_provider_models
SET is_enabled=true,
    routing_enabled=true,
    meta_json=COALESCE(meta_json,'{}'::jsonb) || jsonb_build_object(
      'routing_contract','2026-09-29',
      'eligibility','locale_capability_driven'
    )
WHERE (provider_code='azure' AND model_code='speech_standard_neural')
   OR (provider_code='elevenlabs' AND model_code IN ('eleven_flash_v2_5','eleven_multilingual_v2','eleven_v3'))
   OR (provider_code='sarvam' AND model_code='bulbul_v3');

-- Sarvam Bulbul v3 current official locale capability set.
-- This does not hardcode routing in application source; it certifies masterdata.
CREATE TEMP TABLE _sarvam_supported_locales(locale text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _sarvam_supported_locales(locale) VALUES
  ('en-IN'),('hi-IN'),('bn-IN'),('ta-IN'),('te-IN'),('kn-IN'),
  ('ml-IN'),('mr-IN'),('gu-IN'),('pa-IN'),('or-IN');

UPDATE public.tts_model_locale_capabilities mlc
SET is_enabled=true,
    is_approved=true,
    source='official_docs',
    source_version='2026-09-29'
FROM _sarvam_supported_locales s
WHERE mlc.provider_code='sarvam'
  AND mlc.model_code='bulbul_v3'
  AND mlc.locale=s.locale;

DO $$
DECLARE missing text;
DECLARE unexpected text;
BEGIN
  SELECT string_agg(s.locale,', ' ORDER BY s.locale)
  INTO missing
  FROM _sarvam_supported_locales s
  LEFT JOIN public.tts_model_locale_capabilities mlc
    ON mlc.provider_code='sarvam'
   AND mlc.model_code='bulbul_v3'
   AND mlc.locale=s.locale
   AND mlc.is_enabled=true
   AND mlc.is_approved=true
  WHERE mlc.locale IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'missing enabled Sarvam Bulbul v3 locale capability: %',missing;
  END IF;

  SELECT string_agg(mlc.locale,', ' ORDER BY mlc.locale)
  INTO unexpected
  FROM public.tts_model_locale_capabilities mlc
  LEFT JOIN _sarvam_supported_locales s ON s.locale=mlc.locale
  WHERE mlc.provider_code='sarvam'
    AND mlc.model_code='bulbul_v3'
    AND mlc.is_enabled=true
    AND mlc.is_approved=true
    AND s.locale IS NULL;

  IF unexpected IS NOT NULL THEN
    RAISE EXCEPTION 'unexpected enabled Sarvam Bulbul v3 locale capability outside certified set: %',unexpected;
  END IF;
END $$;

-- Provider/model cost masterdata. Unit is one 1K-character block.
UPDATE public.tts_provider_cost_profiles
SET is_enabled=false,
    effective_to=COALESCE(effective_to,'2026-09-30 01:20:00+00'::timestamptz)
WHERE is_enabled=true
  AND (
    (provider_code='azure' AND model_code='speech_standard_neural')
    OR (provider_code='elevenlabs' AND model_code IN ('eleven_flash_v2_5','eleven_multilingual_v2','eleven_v3'))
    OR (provider_code='sarvam' AND model_code='bulbul_v3')
  )
  AND effective_from <> '2026-09-30 01:20:00+00'::timestamptz;

INSERT INTO public.tts_provider_cost_profiles(
  provider_code,model_code,unit_type,unit_size,unit_cost,currency,
  effective_from,effective_to,is_enabled,source,meta_json
)
SELECT *
FROM (
  VALUES
    ('azure','speech_standard_neural','1k_chars',1::numeric,0.01600000::numeric,'USD',
     '2026-09-30 01:20:00+00'::timestamptz,NULL::timestamptz,true,'public_pricing_baseline',
     jsonb_build_object('pricing_basis','standard_neural_payg','verified_date','2026-09-29')),
    ('elevenlabs','eleven_flash_v2_5','1k_chars',1::numeric,0.05000000::numeric,'USD',
     '2026-09-30 01:20:00+00'::timestamptz,NULL::timestamptz,true,'official_api_pricing',
     jsonb_build_object('pricing_basis','flash_turbo_payg','verified_date','2026-09-29')),
    ('elevenlabs','eleven_multilingual_v2','1k_chars',1::numeric,0.10000000::numeric,'USD',
     '2026-09-30 01:20:00+00'::timestamptz,NULL::timestamptz,true,'official_api_pricing',
     jsonb_build_object('pricing_basis','multilingual_payg','verified_date','2026-09-29')),
    ('elevenlabs','eleven_v3','1k_chars',1::numeric,0.10000000::numeric,'USD',
     '2026-09-30 01:20:00+00'::timestamptz,NULL::timestamptz,true,'official_api_pricing',
     jsonb_build_object('pricing_basis','v3_payg','verified_date','2026-09-29')),
    ('sarvam','bulbul_v3','1k_chars',1::numeric,3.00000000::numeric,'INR',
     '2026-09-30 01:20:00+00'::timestamptz,NULL::timestamptz,true,'official_api_pricing',
     jsonb_build_object('pricing_basis','bulbul_v3_realtime','verified_date','2026-09-29'))
) x(provider_code,model_code,unit_type,unit_size,unit_cost,currency,effective_from,effective_to,is_enabled,source,meta_json)
WHERE NOT EXISTS (
  SELECT 1
  FROM public.tts_provider_cost_profiles c
  WHERE c.provider_code=x.provider_code
    AND c.model_code=x.model_code
    AND c.effective_from=x.effective_from
);

-- Pricing service is not provider-aware at settlement time yet. Until it is,
-- use the conservative maximum current routed TTS USD cost: ElevenLabs v2/v3
-- at USD 0.10 / 1K chars. This prevents routing changes from understating COGS.
UPDATE public.pricing_sku_costs
SET is_active=false,
    effective_to=COALESCE(effective_to,'2026-09-30 01:20:00+00'::timestamptz)
WHERE sku_code IN ('AUDIO_TTS_1K_CHARS','AUDIO_MULTI_PERSON')
  AND is_active=true
  AND effective_from <> '2026-09-30 01:20:00+00'::timestamptz;

INSERT INTO public.pricing_sku_costs(
  sku_code,component_code,cost_model,cost_currency,
  variable_cost_money,fixed_monthly_cost_money,assumed_monthly_units,
  is_active,effective_from,effective_to,metadata_json
)
VALUES
(
  'AUDIO_TTS_1K_CHARS',
  'routed_tts_conservative_max',
  'variable','USD',0.10000000,0,0,true,
  '2026-09-30 01:20:00+00'::timestamptz,NULL,
  jsonb_build_object(
    'source','provider_routed_audio_cost_floor',
    'basis','max current routed public TTS rate per 1K chars',
    'providers','azure,elevenlabs,sarvam',
    'verified_date','2026-09-29',
    'replace_with_actual_provider_metering_when_available',true
  )
),
(
  'AUDIO_MULTI_PERSON',
  'routed_tts_conservative_max',
  'variable','USD',0.10000000,0,0,true,
  '2026-09-30 01:20:00+00'::timestamptz,NULL,
  jsonb_build_object(
    'source','provider_routed_audio_cost_floor',
    'basis','max current routed public TTS rate per 1K chars',
    'providers','azure,elevenlabs,sarvam',
    'verified_date','2026-09-29',
    'replace_with_actual_provider_metering_when_available',true
  )
)
ON CONFLICT(sku_code,component_code,effective_from) DO UPDATE SET
  variable_cost_money=EXCLUDED.variable_cost_money,
  is_active=true,
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

DO $$
DECLARE bad integer;
BEGIN
  SELECT count(*) INTO bad
  FROM public.tts_providers p
  JOIN public.tts_provider_models m ON m.provider_code=p.provider_code
  WHERE (
      (p.provider_code='azure' AND m.model_code='speech_standard_neural')
      OR (p.provider_code='elevenlabs' AND m.model_code IN ('eleven_flash_v2_5','eleven_multilingual_v2','eleven_v3'))
      OR (p.provider_code='sarvam' AND m.model_code='bulbul_v3')
    )
    AND (
      p.is_enabled=false OR p.routing_enabled=false
      OR m.is_enabled=false OR m.routing_enabled=false
    );

  IF bad<>0 THEN
    RAISE EXCEPTION 'routed provider/model enablement failures=%',bad;
  END IF;

  SELECT count(*) INTO bad
  FROM public.pricing_sku_costs
  WHERE sku_code IN ('AUDIO_TTS_1K_CHARS','AUDIO_MULTI_PERSON')
    AND is_active=true
    AND effective_from<=now()
    AND (effective_to IS NULL OR effective_to>now())
    AND variable_cost_money < 0.10;

  IF bad<>0 THEN
    RAISE EXCEPTION 'audio pricing COGS conservative floor failures=%',bad;
  END IF;
END $$;

COMMIT;
