BEGIN;

-- desifaces V3 Premium Talking Video launch pricing.
-- Customer billing is based on actual requested/generated duration, not on
-- provider execution segments. Internal segment count and provider selection
-- must never alter the customer's credit rate.

INSERT INTO public.pricing_skus (
  code,
  name,
  unit,
  category,
  provider_hint,
  default_unit_credits,
  status,
  metadata_json
)
VALUES (
  'LONGFORM_TALK_PREMIUM_SECOND',
  'Premium Talking Video - actual second',
  'second',
  'fusion_extension',
  NULL,
  15,
  'active',
  jsonb_build_object(
    'product_family', 'fusion_extension',
    'mode', 'talking_video',
    'quality_tier', 'premium',
    'billing_basis', 'actual_seconds',
    'min_billable_seconds', 10,
    'credits_per_second', 15,
    'platform_neutral', true,
    'provider_neutral', true,
    'billing_entity', 'parent_longform_job',
    'pricing_policy', 'premium_actual_seconds_v1',
    'seed', '20260903_premium_actual_seconds_v1'
  )
)
ON CONFLICT (code) DO UPDATE
SET
  name = EXCLUDED.name,
  unit = EXCLUDED.unit,
  category = EXCLUDED.category,
  provider_hint = EXCLUDED.provider_hint,
  default_unit_credits = EXCLUDED.default_unit_credits,
  status = EXCLUDED.status,
  metadata_json = COALESCE(public.pricing_skus.metadata_json, '{}'::jsonb) || EXCLUDED.metadata_json;

INSERT INTO public.pricing_variants (
  code,
  name,
  category,
  is_active,
  metadata_json
)
VALUES (
  'TALKING_VIDEO_PREMIUM_SECOND',
  'Premium Talking Video - actual seconds',
  'fusion_extension',
  true,
  jsonb_build_object(
    'product_family', 'fusion_extension',
    'mode', 'talking_video',
    'quality_tier', 'premium',
    'billing_basis', 'actual_seconds',
    'qty_param', 'requested_units',
    'min_billable_seconds', 10,
    'credits_per_second', 15,
    'platform_neutral', true,
    'provider_neutral', true,
    'pricing_policy', 'premium_actual_seconds_v1',
    'seed', '20260903_premium_actual_seconds_v1'
  )
)
ON CONFLICT (code) DO UPDATE
SET
  name = EXCLUDED.name,
  category = EXCLUDED.category,
  is_active = EXCLUDED.is_active,
  metadata_json = COALESCE(public.pricing_variants.metadata_json, '{}'::jsonb) || EXCLUDED.metadata_json;

DELETE FROM public.pricing_variant_lines
WHERE variant_code = 'TALKING_VIDEO_PREMIUM_SECOND';

INSERT INTO public.pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_value,
  qty_param,
  metadata_json
)
VALUES (
  'TALKING_VIDEO_PREMIUM_SECOND',
  'LONGFORM_TALK_PREMIUM_SECOND',
  'param',
  NULL,
  'requested_units',
  jsonb_build_object(
    'billing_basis', 'actual_seconds',
    'min_billable_seconds', 10,
    'credits_per_second', 15,
    'platform_neutral', true,
    'provider_neutral', true,
    'billing_entity', 'parent_longform_job',
    'seed', '20260903_premium_actual_seconds_v1'
  )
);

-- Give every existing web/mobile pricebook the identical generation-credit rate.
-- Cash/store pricing can differ elsewhere; generation credit consumption cannot.
INSERT INTO public.pricing_sku_prices (
  pricebook_id,
  sku_code,
  unit_credits_override,
  unit_money_override,
  min_qty,
  max_qty,
  metadata_json
)
SELECT
  pb.id,
  'LONGFORM_TALK_PREMIUM_SECOND',
  15,
  NULL,
  10,
  NULL,
  jsonb_build_object(
    'billing_basis', 'actual_seconds',
    'min_billable_seconds', 10,
    'credits_per_second', 15,
    'platform_neutral', true,
    'provider_neutral', true,
    'pricing_policy', 'premium_actual_seconds_v1',
    'seed', '20260903_premium_actual_seconds_v1'
  )
FROM public.pricing_pricebooks pb
WHERE pb.channel IN ('web', 'mobile')
ON CONFLICT (pricebook_id, sku_code) DO UPDATE
SET
  unit_credits_override = 15,
  unit_money_override = NULL,
  min_qty = 10,
  max_qty = NULL,
  metadata_json = COALESCE(public.pricing_sku_prices.metadata_json, '{}'::jsonb) || EXCLUDED.metadata_json;

DO $$
DECLARE
  bad_count integer;
BEGIN
  SELECT count(*) INTO bad_count
  FROM public.pricing_sku_prices sp
  JOIN public.pricing_pricebooks pb ON pb.id = sp.pricebook_id
  WHERE sp.sku_code = 'LONGFORM_TALK_PREMIUM_SECOND'
    AND pb.channel IN ('web','mobile')
    AND (
      sp.unit_credits_override <> 15
      OR sp.unit_money_override IS NOT NULL
      OR COALESCE(sp.min_qty, 0) <> 10
    );

  IF bad_count > 0 THEN
    RAISE EXCEPTION 'Premium actual-second pricing parity verification failed for % pricebook rows', bad_count;
  END IF;
END $$;

COMMIT;
