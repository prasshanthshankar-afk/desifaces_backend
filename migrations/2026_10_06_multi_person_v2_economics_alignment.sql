BEGIN;

-- desifaces multi-person v2 pricing/economics alignment.
-- Canonical values are the DEV-certified catalog from 2026-10-06.
-- Scope: relevant Face/Audio/Fusion base SKUs, multi-person SKUs,
-- active web/mobile pricebook rows, variants/variant-lines, and multi-person COGS.
-- Group Photo Conversation remains 18 credits/actual second with OmniHuman COGS 0.16 USD/sec.

SELECT pg_advisory_xact_lock(hashtext('desifaces_multi_person_v2_economics'));

DO $$
BEGIN
  IF to_regclass('public.pricing_skus') IS NULL
     OR to_regclass('public.pricing_variants') IS NULL
     OR to_regclass('public.pricing_variant_lines') IS NULL
     OR to_regclass('public.pricing_sku_prices') IS NULL
     OR to_regclass('public.pricing_pricebooks') IS NULL
     OR to_regclass('public.pricing_sku_costs') IS NULL THEN
    RAISE EXCEPTION 'required pricing tables are missing';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM public.pricing_skus WHERE code='GROUP_TALK_PREMIUM_SECOND' AND status='active') THEN
    RAISE EXCEPTION 'GROUP_TALK_PREMIUM_SECOND must already exist and be active';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Canonical base credit values used by the DEV-certified v2 catalog.
-- These rows intentionally align the relevant pricing family, not the full
-- pricing database.
-- ---------------------------------------------------------------------------

UPDATE public.pricing_skus SET default_unit_credits=34
 WHERE code='IMG_STD_RUN' AND status='active';
UPDATE public.pricing_skus SET default_unit_credits=46
 WHERE code='FACE_EDIT_PREMIUM_RUN' AND status='active';
UPDATE public.pricing_skus SET default_unit_credits=16
 WHERE code='AUDIO_TTS_1K_CHARS' AND status='active';
UPDATE public.pricing_skus SET default_unit_credits=1441
 WHERE code='FUSION_TALK_MIN' AND status='active';
UPDATE public.pricing_skus SET default_unit_credits=15
 WHERE code='LONGFORM_TALK_PREMIUM_SECOND' AND status='active';

-- Keep active web/mobile source pricebook credit rows aligned with DEV.
UPDATE public.pricing_sku_prices sp
SET unit_credits_override = CASE sp.sku_code
      WHEN 'IMG_STD_RUN' THEN 34
      WHEN 'FACE_EDIT_PREMIUM_RUN' THEN 46
      WHEN 'AUDIO_TTS_1K_CHARS' THEN 16
      WHEN 'FUSION_TALK_MIN' THEN 1441
      WHEN 'LONGFORM_TALK_PREMIUM_SECOND' THEN 15
    END,
    unit_money_override = NULL
FROM public.pricing_pricebooks pb
WHERE pb.id=sp.pricebook_id
  AND pb.is_active=true
  AND pb.channel IN ('web','mobile')
  AND sp.sku_code IN (
    'IMG_STD_RUN',
    'FACE_EDIT_PREMIUM_RUN',
    'AUDIO_TTS_1K_CHARS',
    'FUSION_TALK_MIN',
    'LONGFORM_TALK_PREMIUM_SECOND'
  );

-- ---------------------------------------------------------------------------
-- Canonical multi-person v2 SKUs.
-- ---------------------------------------------------------------------------

INSERT INTO public.pricing_skus (
  code,name,unit,category,provider_hint,default_unit_credits,
  status,effective_from,effective_to,metadata_json
)
SELECT
  'FACE_MULTI_PERSON',
  'Face Studio - Multi-Person T2I Premium',
  src.unit,'face',src.provider_hint,41,
  'active',now(),NULL,
  COALESCE(src.metadata_json,'{}'::jsonb) || jsonb_build_object(
    'multi_person',true,
    'premium',true,
    'source_sku','IMG_STD_RUN',
    'premium_rate_multiplier',1.20,
    'multi_person_surcharge_pct',20,
    'pricing_policy','multi_person_workload_v2',
    'pricing_basis','base_plus_20_percent',
    'participant_count_in_sku',false
  )
FROM public.pricing_skus src
WHERE src.code='IMG_STD_RUN'
ON CONFLICT (code) DO UPDATE SET
  name=EXCLUDED.name,
  unit=EXCLUDED.unit,
  category=EXCLUDED.category,
  provider_hint=EXCLUDED.provider_hint,
  default_unit_credits=EXCLUDED.default_unit_credits,
  status='active',
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

INSERT INTO public.pricing_skus (
  code,name,unit,category,provider_hint,default_unit_credits,
  status,effective_from,effective_to,metadata_json
)
SELECT
  'FACE_MULTI_PERSON_I2I',
  'Face Studio - Multi-Person I2I Premium',
  src.unit,'face',src.provider_hint,56,
  'active',now(),NULL,
  COALESCE(src.metadata_json,'{}'::jsonb) || jsonb_build_object(
    'multi_person',true,
    'premium',true,
    'source_sku','FACE_EDIT_PREMIUM_RUN',
    'premium_rate_multiplier',1.20,
    'multi_person_surcharge_pct',20,
    'pricing_policy','multi_person_workload_v2',
    'pricing_basis','base_plus_20_percent',
    'participant_count_in_sku',false
  )
FROM public.pricing_skus src
WHERE src.code='FACE_EDIT_PREMIUM_RUN'
ON CONFLICT (code) DO UPDATE SET
  name=EXCLUDED.name,
  unit=EXCLUDED.unit,
  category=EXCLUDED.category,
  provider_hint=EXCLUDED.provider_hint,
  default_unit_credits=EXCLUDED.default_unit_credits,
  status='active',
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

INSERT INTO public.pricing_skus (
  code,name,unit,category,provider_hint,default_unit_credits,
  status,effective_from,effective_to,metadata_json
)
SELECT
  'AUDIO_MULTI_PERSON',
  'Audio Studio - Multi-Person Premium',
  src.unit,'audio',src.provider_hint,16,
  'active',now(),NULL,
  (COALESCE(src.metadata_json,'{}'::jsonb) - 'premium_rate_multiplier')
    || jsonb_build_object(
      'multi_person',true,
      'premium',true,
      'source_sku','AUDIO_TTS_1K_CHARS',
      'multi_person_surcharge_pct',0,
      'pricing_policy','multi_person_workload_v2',
      'pricing_basis','aggregate_natural_usage',
      'participant_count_in_sku',false
    )
FROM public.pricing_skus src
WHERE src.code='AUDIO_TTS_1K_CHARS'
ON CONFLICT (code) DO UPDATE SET
  name=EXCLUDED.name,
  unit=EXCLUDED.unit,
  category=EXCLUDED.category,
  provider_hint=EXCLUDED.provider_hint,
  default_unit_credits=EXCLUDED.default_unit_credits,
  status='active',
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

INSERT INTO public.pricing_skus (
  code,name,unit,category,provider_hint,default_unit_credits,
  status,effective_from,effective_to,metadata_json
)
SELECT
  'FUSION_MULTI_PERSON',
  'Fusion Studio - Multi-Person Premium',
  src.unit,'fusion',src.provider_hint,1730,
  'active',now(),NULL,
  COALESCE(src.metadata_json,'{}'::jsonb) || jsonb_build_object(
    'multi_person',true,
    'premium',true,
    'source_sku','FUSION_TALK_MIN',
    'premium_rate_multiplier',1.20,
    'multi_person_surcharge_pct',20,
    'pricing_policy','multi_person_workload_v2',
    'pricing_basis','base_plus_20_percent',
    'participant_count_in_sku',false
  )
FROM public.pricing_skus src
WHERE src.code='FUSION_TALK_MIN'
ON CONFLICT (code) DO UPDATE SET
  name=EXCLUDED.name,
  unit=EXCLUDED.unit,
  category=EXCLUDED.category,
  provider_hint=EXCLUDED.provider_hint,
  default_unit_credits=EXCLUDED.default_unit_credits,
  status='active',
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- Canonical variants and quantity contracts.
-- ---------------------------------------------------------------------------

INSERT INTO public.pricing_variants (code,name,category,is_active,metadata_json)
VALUES
  (
    'FACE_MULTI_PERSON',
    'Face Studio - Multi-Person T2I Premium',
    'face',true,
    '{"multi_person":true,"pricing_policy":"multi_person_workload_v2","source_sku":"IMG_STD_RUN","qty_param":"num_edits"}'::jsonb
  ),
  (
    'FACE_MULTI_PERSON_I2I',
    'Face Studio - Multi-Person I2I Premium',
    'face',true,
    '{"multi_person":true,"pricing_policy":"multi_person_workload_v2","source_sku":"FACE_EDIT_PREMIUM_RUN","qty_param":"num_edits"}'::jsonb
  ),
  (
    'AUDIO_MULTI_PERSON',
    'Audio Studio - Multi-Person Premium',
    'audio',true,
    '{"multi_person":true,"pricing_policy":"multi_person_workload_v2","source_sku":"AUDIO_TTS_1K_CHARS","qty_param":"chars_1k","pricing_basis":"aggregate_natural_usage"}'::jsonb
  ),
  (
    'FUSION_MULTI_PERSON',
    'Fusion Studio - Multi-Person Premium',
    'fusion',true,
    '{"multi_person":true,"pricing_policy":"multi_person_workload_v2","source_sku":"FUSION_TALK_MIN","qty_param":"minutes"}'::jsonb
  )
ON CONFLICT (code) DO UPDATE SET
  name=EXCLUDED.name,
  category=EXCLUDED.category,
  is_active=true,
  metadata_json=EXCLUDED.metadata_json;

DELETE FROM public.pricing_variant_lines
WHERE variant_code IN (
  'FACE_MULTI_PERSON',
  'FACE_MULTI_PERSON_I2I',
  'AUDIO_MULTI_PERSON',
  'FUSION_MULTI_PERSON'
);

INSERT INTO public.pricing_variant_lines
  (variant_code,sku_code,qty_mode,qty_value,qty_param,metadata_json)
VALUES
  ('FACE_MULTI_PERSON','FACE_MULTI_PERSON','param',NULL,'num_edits','{"pricing_policy":"multi_person_workload_v2"}'::jsonb),
  ('FACE_MULTI_PERSON_I2I','FACE_MULTI_PERSON_I2I','param',NULL,'num_edits','{"pricing_policy":"multi_person_workload_v2"}'::jsonb),
  ('AUDIO_MULTI_PERSON','AUDIO_MULTI_PERSON','param',NULL,'chars_1k','{"pricing_policy":"multi_person_workload_v2","pricing_basis":"aggregate_natural_usage"}'::jsonb),
  ('FUSION_MULTI_PERSON','FUSION_MULTI_PERSON','param',NULL,'minutes','{"pricing_policy":"multi_person_workload_v2"}'::jsonb);

-- ---------------------------------------------------------------------------
-- Active web/mobile multi-person pricebook rows. Credits are canonical and
-- currency-neutral; money overrides remain NULL as in DEV.
-- ---------------------------------------------------------------------------

WITH target(sku_code,source_sku,credits,multiplier) AS (
  VALUES
    ('FACE_MULTI_PERSON','IMG_STD_RUN',41::bigint,1.20::numeric),
    ('FACE_MULTI_PERSON_I2I','FACE_EDIT_PREMIUM_RUN',56::bigint,1.20::numeric),
    ('AUDIO_MULTI_PERSON','AUDIO_TTS_1K_CHARS',16::bigint,1.00::numeric),
    ('FUSION_MULTI_PERSON','FUSION_TALK_MIN',1730::bigint,1.20::numeric)
)
INSERT INTO public.pricing_sku_prices (
  pricebook_id,sku_code,unit_credits_override,unit_money_override,
  min_qty,max_qty,metadata_json
)
SELECT
  src.pricebook_id,
  t.sku_code,
  t.credits,
  NULL,
  1,
  NULL,
  CASE
    WHEN t.sku_code='AUDIO_MULTI_PERSON' THEN
      jsonb_build_object(
        'multi_person',true,
        'source_sku',t.source_sku,
        'multi_person_surcharge_pct',0,
        'pricing_policy','multi_person_workload_v2',
        'pricing_basis','aggregate_natural_usage'
      )
    ELSE
      jsonb_build_object(
        'multi_person',true,
        'source_sku',t.source_sku,
        'premium_rate_multiplier',t.multiplier,
        'multi_person_surcharge_pct',20,
        'pricing_policy','multi_person_workload_v2',
        'pricing_basis','base_plus_20_percent'
      )
  END
FROM target t
JOIN public.pricing_sku_prices src ON src.sku_code=t.source_sku
JOIN public.pricing_pricebooks pb ON pb.id=src.pricebook_id
WHERE pb.is_active=true
  AND pb.channel IN ('web','mobile')
ON CONFLICT (pricebook_id,sku_code) DO UPDATE SET
  unit_credits_override=EXCLUDED.unit_credits_override,
  unit_money_override=NULL,
  min_qty=1,
  max_qty=NULL,
  metadata_json=EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- Canonical multi-person COGS from DEV.
-- End competing active components first to avoid double counting.
-- ---------------------------------------------------------------------------

UPDATE public.pricing_sku_costs
SET is_active=false,
    effective_to=COALESCE(effective_to,'2026-10-06 00:00:00+00'::timestamptz)
WHERE sku_code='FACE_MULTI_PERSON'
  AND component_code<>'openai_gpt_image_2_high_conservative'
  AND is_active=true;

UPDATE public.pricing_sku_costs
SET is_active=false,
    effective_to=COALESCE(effective_to,'2026-10-06 00:00:00+00'::timestamptz)
WHERE sku_code='FACE_MULTI_PERSON_I2I'
  AND component_code<>'openai_gpt_image_2_edit_conservative'
  AND is_active=true;

UPDATE public.pricing_sku_costs
SET is_active=false,
    effective_to=COALESCE(effective_to,'2026-10-06 00:00:00+00'::timestamptz)
WHERE sku_code='AUDIO_MULTI_PERSON'
  AND component_code<>'routed_tts_conservative_max'
  AND is_active=true;

UPDATE public.pricing_sku_costs
SET is_active=false,
    effective_to=COALESCE(effective_to,'2026-10-06 00:00:00+00'::timestamptz)
WHERE sku_code='FUSION_MULTI_PERSON'
  AND component_code<>'parent_retry_failover_reserve'
  AND is_active=true;

INSERT INTO public.pricing_sku_costs (
  sku_code,component_code,cost_model,cost_currency,
  variable_cost_money,fixed_monthly_cost_money,assumed_monthly_units,
  is_active,effective_from,effective_to,metadata_json
)
VALUES
(
  'FACE_MULTI_PERSON','openai_gpt_image_2_high_conservative','variable','USD',
  0.22000000,0,0,true,'2026-09-30 00:15:00+00',NULL,
  '{"basis":"same provider generation cost as T2I; premium is customer-facing engineering value","source":"commercial_alignment_2026_09_29_v4","verified_date":"2026-09-29","customer_pricing_impact":false,"replace_with_contract_or_metered_rate_when_available":true}'::jsonb
),
(
  'FACE_MULTI_PERSON_I2I','openai_gpt_image_2_edit_conservative','variable','USD',
  0.30000000,0,0,true,'2026-09-30 00:15:00+00',NULL,
  '{"basis":"GPT Image 2 high output plus high-fidelity image-input and prompt allowance","source":"commercial_alignment_2026_09_29_v4","verified_date":"2026-09-29","customer_pricing_impact":false,"replace_with_contract_or_metered_rate_when_available":true}'::jsonb
),
(
  'AUDIO_MULTI_PERSON','routed_tts_conservative_max','variable','USD',
  0.10000000,0,0,true,'2026-09-30 01:20:00+00',NULL,
  '{"basis":"max current routed public TTS rate per 1K chars","source":"provider_routed_audio_cost_floor","providers":"azure,elevenlabs,sarvam","verified_date":"2026-09-29","replace_with_actual_provider_metering_when_available":true}'::jsonb
),
(
  'FUSION_MULTI_PERSON','parent_retry_failover_reserve','variable','USD',
  13.50000000,0,0,true,'2026-09-30 00:15:00+00',NULL,
  '{"basis":"provider-neutral parent reserve above current Sync3 normal minute cost with retry/failover allowance","source":"commercial_alignment_2026_09_29_v4","verified_date":"2026-09-29","customer_pricing_impact":false,"replace_with_contract_or_metered_rate_when_available":true}'::jsonb
)
ON CONFLICT (sku_code,component_code,effective_from) DO UPDATE SET
  cost_model=EXCLUDED.cost_model,
  cost_currency=EXCLUDED.cost_currency,
  variable_cost_money=EXCLUDED.variable_cost_money,
  fixed_monthly_cost_money=EXCLUDED.fixed_monthly_cost_money,
  assumed_monthly_units=EXCLUDED.assumed_monthly_units,
  is_active=true,
  effective_to=NULL,
  metadata_json=EXCLUDED.metadata_json;

-- Preserve and certify the already-launched Group Photo Conversation contract.
DO $$
DECLARE
  group_credits bigint;
  group_cost numeric;
BEGIN
  SELECT default_unit_credits INTO group_credits
  FROM public.pricing_skus
  WHERE code='GROUP_TALK_PREMIUM_SECOND';

  SELECT variable_cost_money INTO group_cost
  FROM public.pricing_sku_costs
  WHERE sku_code='GROUP_TALK_PREMIUM_SECOND'
    AND component_code='fal_omnihuman_variable'
    AND is_active=true
    AND effective_from<=now()
    AND (effective_to IS NULL OR effective_to>now())
  ORDER BY effective_from DESC
  LIMIT 1;

  IF group_credits IS DISTINCT FROM 18 THEN
    RAISE EXCEPTION 'GROUP_TALK_PREMIUM_SECOND expected 18 credits, got %', group_credits;
  END IF;
  IF group_cost IS DISTINCT FROM 0.16000000::numeric THEN
    RAISE EXCEPTION 'GROUP_TALK_PREMIUM_SECOND expected 0.16 COGS, got %', group_cost;
  END IF;
END $$;

-- Final fail-closed contract.
DO $$
BEGIN
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='IMG_STD_RUN') <> 34 THEN
    RAISE EXCEPTION 'IMG_STD_RUN != 34';
  END IF;
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='FACE_EDIT_PREMIUM_RUN') <> 46 THEN
    RAISE EXCEPTION 'FACE_EDIT_PREMIUM_RUN != 46';
  END IF;
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='AUDIO_TTS_1K_CHARS') <> 16 THEN
    RAISE EXCEPTION 'AUDIO_TTS_1K_CHARS != 16';
  END IF;
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='FUSION_TALK_MIN') <> 1441 THEN
    RAISE EXCEPTION 'FUSION_TALK_MIN != 1441';
  END IF;
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='FACE_MULTI_PERSON') <> 41 THEN
    RAISE EXCEPTION 'FACE_MULTI_PERSON != 41';
  END IF;
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='FACE_MULTI_PERSON_I2I') <> 56 THEN
    RAISE EXCEPTION 'FACE_MULTI_PERSON_I2I != 56';
  END IF;
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='AUDIO_MULTI_PERSON') <> 16 THEN
    RAISE EXCEPTION 'AUDIO_MULTI_PERSON != 16';
  END IF;
  IF (SELECT default_unit_credits FROM public.pricing_skus WHERE code='FUSION_MULTI_PERSON') <> 1730 THEN
    RAISE EXCEPTION 'FUSION_MULTI_PERSON != 1730';
  END IF;
END $$;

COMMIT;
