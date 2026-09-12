-- desifaces V3 production-readiness economics correction.
-- INTERNAL COGS ONLY. This migration does not change customer prices, credits,
-- entitlements, checkout amounts, or billing behavior.
--
-- AUDIO_TTS_1K_CHARS is priced/billed in 1K-character units and routes to
-- Azure neural TTS. The production cost model must therefore carry a non-zero
-- variable provider cost in the same unit. Baseline PAYG standard-neural rate:
-- USD 16 / 1,000,000 billable characters = USD 0.016 / 1K characters.
--
-- If desifaces has a negotiated Azure commitment/enterprise rate, update this
-- cost component through a later effective-dated migration. Never silently set
-- a paid provider SKU to zero COGS.

BEGIN;

DO $$
BEGIN
  IF to_regclass('public.pricing_skus') IS NULL THEN
    RAISE EXCEPTION 'pricing_skus table is required';
  END IF;
  IF to_regclass('public.pricing_sku_costs') IS NULL THEN
    RAISE EXCEPTION 'pricing_sku_costs table is required';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.pricing_skus
    WHERE code = 'AUDIO_TTS_1K_CHARS' AND status = 'active'
  ) THEN
    RAISE EXCEPTION 'active AUDIO_TTS_1K_CHARS SKU is required';
  END IF;
END $$;

-- End any older active version of this same provider-cost component so that
-- cost lookup cannot double count it.
UPDATE public.pricing_sku_costs
   SET is_active = false,
       effective_to = COALESCE(effective_to, '2026-09-12 00:00:00+00'::timestamptz)
 WHERE sku_code = 'AUDIO_TTS_1K_CHARS'
   AND component_code = 'azure_neural_tts_payg'
   AND effective_from <> '2026-09-12 00:00:00+00'::timestamptz
   AND is_active = true;

INSERT INTO public.pricing_sku_costs (
  sku_code,
  component_code,
  cost_model,
  cost_currency,
  variable_cost_money,
  fixed_monthly_cost_money,
  assumed_monthly_units,
  is_active,
  effective_from,
  effective_to,
  metadata_json
) VALUES (
  'AUDIO_TTS_1K_CHARS',
  'azure_neural_tts_payg',
  'variable',
  'USD',
  0.01600000,
  0,
  0,
  true,
  '2026-09-12 00:00:00+00'::timestamptz,
  NULL,
  jsonb_build_object(
    'provider', 'azure_tts',
    'service', 'Azure AI Speech Text to Speech',
    'voice_class', 'standard_neural',
    'pricing_basis', 'pay_as_you_go_per_character',
    'rate_usd_per_1m_characters', 16,
    'rate_usd_per_1k_characters', 0.016,
    'sku_unit', '1k_chars',
    'source', 'azure_public_pricing_baseline',
    'verified_date', '2026-09-12',
    'production_rule', 'replace_with_contract_rate_via_effective_dated_migration_if_applicable',
    'customer_pricing_impact', false
  )
)
ON CONFLICT (sku_code, component_code, effective_from)
DO UPDATE SET
  cost_model = EXCLUDED.cost_model,
  cost_currency = EXCLUDED.cost_currency,
  variable_cost_money = EXCLUDED.variable_cost_money,
  fixed_monthly_cost_money = EXCLUDED.fixed_monthly_cost_money,
  assumed_monthly_units = EXCLUDED.assumed_monthly_units,
  is_active = EXCLUDED.is_active,
  effective_to = EXCLUDED.effective_to,
  metadata_json = EXCLUDED.metadata_json;

DO $$
DECLARE
  v_cost numeric;
BEGIN
  SELECT variable_cost_money
    INTO v_cost
    FROM public.pricing_sku_costs
   WHERE sku_code = 'AUDIO_TTS_1K_CHARS'
     AND component_code = 'azure_neural_tts_payg'
     AND is_active = true
     AND effective_to IS NULL
   ORDER BY effective_from DESC
   LIMIT 1;

  IF v_cost IS NULL OR v_cost <= 0 THEN
    RAISE EXCEPTION 'AUDIO_TTS COGS must be configured and > 0';
  END IF;
END $$;

COMMIT;
