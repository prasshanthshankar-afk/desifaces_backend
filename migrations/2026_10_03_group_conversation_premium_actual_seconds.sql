BEGIN;

-- desifaces Group Photo Conversation premium video pricing.
-- Base Premium Talking Video is 15 credits/second. Multi-person video carries
-- the launch-approved +20% surcharge => 18 credits/second.
-- Customer billing remains based on actual requested/generated duration;
-- provider segmentation and internal child count never change customer units.

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
  'GROUP_TALK_PREMIUM_SECOND',
  'Group Conversation Premium Video - actual second',
  'second',
  'fusion_extension',
  NULL,
  18,
  'active',
  jsonb_build_object(
    'product_family', 'fusion_extension',
    'mode', 'group_photo_conversation',
    'quality_tier', 'premium',
    'billing_basis', 'actual_seconds',
    'min_billable_seconds', 10,
    'base_credits_per_second', 15,
    'credits_per_second', 18,
    'multi_person_surcharge_pct', 20,
    'platform_neutral', true,
    'provider_neutral', true,
    'billing_entity', 'parent_v3_scene',
    'pricing_policy', 'group_conversation_premium_actual_seconds_v1',
    'seed', '20261003_group_conversation_premium_actual_seconds_v1'
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
  'GROUP_TALKING_VIDEO_PREMIUM_SECOND',
  'Group Conversation Premium Video - actual seconds',
  'fusion_extension',
  true,
  jsonb_build_object(
    'product_family', 'fusion_extension',
    'mode', 'group_photo_conversation',
    'quality_tier', 'premium',
    'billing_basis', 'actual_seconds',
    'qty_param', 'requested_units',
    'min_billable_seconds', 10,
    'base_credits_per_second', 15,
    'credits_per_second', 18,
    'multi_person_surcharge_pct', 20,
    'platform_neutral', true,
    'provider_neutral', true,
    'pricing_policy', 'group_conversation_premium_actual_seconds_v1',
    'seed', '20261003_group_conversation_premium_actual_seconds_v1'
  )
)
ON CONFLICT (code) DO UPDATE
SET
  name = EXCLUDED.name,
  category = EXCLUDED.category,
  is_active = EXCLUDED.is_active,
  metadata_json = COALESCE(public.pricing_variants.metadata_json, '{}'::jsonb) || EXCLUDED.metadata_json;

DELETE FROM public.pricing_variant_lines
WHERE variant_code = 'GROUP_TALKING_VIDEO_PREMIUM_SECOND';

INSERT INTO public.pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_value,
  qty_param,
  metadata_json
)
VALUES (
  'GROUP_TALKING_VIDEO_PREMIUM_SECOND',
  'GROUP_TALK_PREMIUM_SECOND',
  'param',
  NULL,
  'requested_units',
  jsonb_build_object(
    'billing_basis', 'actual_seconds',
    'min_billable_seconds', 10,
    'base_credits_per_second', 15,
    'credits_per_second', 18,
    'multi_person_surcharge_pct', 20,
    'platform_neutral', true,
    'provider_neutral', true,
    'billing_entity', 'parent_v3_scene',
    'seed', '20261003_group_conversation_premium_actual_seconds_v1'
  )
);

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
  'GROUP_TALK_PREMIUM_SECOND',
  18,
  NULL,
  10,
  NULL,
  jsonb_build_object(
    'billing_basis', 'actual_seconds',
    'min_billable_seconds', 10,
    'base_credits_per_second', 15,
    'credits_per_second', 18,
    'multi_person_surcharge_pct', 20,
    'platform_neutral', true,
    'provider_neutral', true,
    'pricing_policy', 'group_conversation_premium_actual_seconds_v1',
    'seed', '20261003_group_conversation_premium_actual_seconds_v1'
  )
FROM public.pricing_pricebooks pb
WHERE pb.channel IN ('web', 'mobile')
ON CONFLICT (pricebook_id, sku_code) DO UPDATE
SET
  unit_credits_override = EXCLUDED.unit_credits_override,
  unit_money_override = EXCLUDED.unit_money_override,
  min_qty = EXCLUDED.min_qty,
  max_qty = EXCLUDED.max_qty,
  metadata_json = COALESCE(public.pricing_sku_prices.metadata_json, '{}'::jsonb) || EXCLUDED.metadata_json;

DO $$
DECLARE
  bad_count integer;
  missing_channels integer;
BEGIN
  SELECT count(*) INTO bad_count
  FROM public.pricing_sku_prices sp
  JOIN public.pricing_pricebooks pb ON pb.id = sp.pricebook_id
  WHERE sp.sku_code = 'GROUP_TALK_PREMIUM_SECOND'
    AND pb.channel IN ('web','mobile')
    AND (
      sp.unit_credits_override <> 18
      OR sp.unit_money_override IS NOT NULL
      OR COALESCE(sp.min_qty, 0) <> 10
    );

  IF bad_count > 0 THEN
    RAISE EXCEPTION 'Group conversation +20%% pricing verification failed for % pricebook rows', bad_count;
  END IF;

  SELECT count(DISTINCT pb.channel) INTO missing_channels
  FROM public.pricing_sku_prices sp
  JOIN public.pricing_pricebooks pb ON pb.id = sp.pricebook_id
  WHERE sp.sku_code = 'GROUP_TALK_PREMIUM_SECOND'
    AND pb.channel IN ('web','mobile');

  IF missing_channels < 2 THEN
    RAISE EXCEPTION 'Group conversation pricing requires both web and mobile pricebooks';
  END IF;
END $$;

COMMIT;
