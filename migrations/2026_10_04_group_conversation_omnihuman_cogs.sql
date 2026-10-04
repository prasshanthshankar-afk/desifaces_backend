BEGIN;

-- Internal COGS for the frozen Group Photo Conversation provider.
-- Customer billing remains provider-neutral at 18 credits/actual second (+20%
-- over the 15-credit premium talking-video base). This row only supplies
-- economics/margin telemetry for OmniHuman 1.5 and never changes customer price.

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
)
VALUES (
  'GROUP_TALK_PREMIUM_SECOND',
  'fal_omnihuman_v15_variable',
  'variable',
  'USD',
  0.16000000,
  0,
  0,
  true,
  '2026-10-04 00:00:00+00',
  NULL,
  jsonb_build_object(
    'provider', 'omnihuman_v15',
    'provider_model', 'fal-ai/bytedance/omnihuman/v1.5',
    'billing_unit', 'generated_second',
    'source', 'fal_public_rate_2026_10_04',
    'customer_billing_unchanged', true,
    'notes', 'SAM2 speaker-mask generation is orchestration overhead and is not separately billed to the customer.'
  )
)
ON CONFLICT (sku_code, component_code, effective_from) DO UPDATE
SET
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
  active_cost numeric(18,8);
BEGIN
  SELECT variable_cost_money
    INTO active_cost
  FROM public.pricing_sku_costs
  WHERE sku_code='GROUP_TALK_PREMIUM_SECOND'
    AND component_code='fal_omnihuman_v15_variable'
    AND is_active=true
    AND effective_from <= now()
    AND (effective_to IS NULL OR effective_to > now())
  ORDER BY effective_from DESC
  LIMIT 1;

  IF active_cost IS DISTINCT FROM 0.16000000::numeric THEN
    RAISE EXCEPTION 'OmniHuman group conversation COGS certification failed: %', active_cost;
  END IF;
END $$;

COMMIT;
