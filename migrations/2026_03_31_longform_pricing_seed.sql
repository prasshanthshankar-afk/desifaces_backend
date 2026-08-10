
BEGIN;

-- =========================================================
-- DesiFaces longform pricing seed
-- Covers:
--   1) pricing_skus
--   2) pricing_variants
--   3) pricing_variant_lines
--   4) pricing_pricebooks
--   5) pricing_sku_prices
--   6) pricing_tier_prices
--   7) pricing_fx_rates
--
-- Assumptions:
--   - Existing pricing_tiers codes: pro, business
--   - Currency presentment rule:
--       * India => INR
--       * Rest of world => USD
--   - Safer launch:
--       * free / creator: no longform sell price rows here
--       * pro / business: seeded
--       * enterprise: contract/custom, not seeded here
--
-- This file is idempotent.
-- =========================================================


-- ---------------------------------------------------------
-- 1) SKUs
-- ---------------------------------------------------------
INSERT INTO pricing_skus (
  code,
  name,
  unit,
  category,
  provider_hint,
  default_unit_credits,
  status,
  metadata_json
)
VALUES
  (
    'LONGFORM_TALK_MIN',
    'Talking Video (per minute)',
    'minute',
    'fusion_extension',
    'omnihuman',
    100,
    'active',
    jsonb_build_object(
      'longform_profile', 'talking_video',
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16'),
      'notes', 'Lower-cost longform presenter product'
    )
  ),
  (
    'LONGFORM_CINEMATIC_MIN',
    'Cinematic Video Direction (per minute)',
    'minute',
    'fusion_extension',
    'omnihuman+luma+kling',
    300,
    'active',
    jsonb_build_object(
      'longform_profile', 'cinematic_video_direction',
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16'),
      'notes', 'Premium longform cinematic product'
    )
  )
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  unit = EXCLUDED.unit,
  category = EXCLUDED.category,
  provider_hint = EXCLUDED.provider_hint,
  default_unit_credits = EXCLUDED.default_unit_credits,
  status = EXCLUDED.status,
  metadata_json = EXCLUDED.metadata_json;


-- ---------------------------------------------------------
-- 2) Variants
-- ---------------------------------------------------------
INSERT INTO pricing_variants (
  code,
  name,
  category,
  is_active,
  metadata_json
)
VALUES
  (
    'TALKING_VIDEO',
    'Talking Video',
    'fusion_extension',
    true,
    jsonb_build_object(
      'longform_profile', 'talking_video',
      'premium', false,
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16'),
      'pricing_notes', 'Presenter-first stitched longform video'
    )
  ),
  (
    'CINEMATIC_VIDEO_DIRECTION',
    'Cinematic Video Direction',
    'fusion_extension',
    true,
    jsonb_build_object(
      'longform_profile', 'cinematic_video_direction',
      'premium', true,
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16'),
      'pricing_notes', 'Premium choreographed cinematic longform video'
    )
  )
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  category = EXCLUDED.category,
  is_active = EXCLUDED.is_active,
  metadata_json = EXCLUDED.metadata_json;


-- ---------------------------------------------------------
-- 3) Variant lines
-- ---------------------------------------------------------
INSERT INTO pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_param,
  metadata_json
)
VALUES
  (
    'TALKING_VIDEO',
    'LONGFORM_TALK_MIN',
    'param',
    'minutes',
    jsonb_build_object('longform_profile', 'talking_video')
  ),
  (
    'CINEMATIC_VIDEO_DIRECTION',
    'LONGFORM_CINEMATIC_MIN',
    'param',
    'minutes',
    jsonb_build_object('longform_profile', 'cinematic_video_direction')
  )
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE SET
  metadata_json = EXCLUDED.metadata_json;


-- ---------------------------------------------------------
-- 4) Pricebooks
-- Use deterministic UUIDs so the file stays idempotent.
-- Global USD = country_code NULL
-- India INR = country_code 'IN'
-- channel = 'default'
-- ---------------------------------------------------------
INSERT INTO pricing_pricebooks (
  id,
  name,
  country_code,
  currency,
  channel,
  tier_code,
  multiplier,
  is_active,
  effective_from,
  metadata_json
)
VALUES
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b001',
    'PRO_USD_GLOBAL_DEFAULT',
    NULL,
    'USD',
    'default',
    'pro',
    1.0,
    true,
    '2026-03-31 00:00:00+00',
    jsonb_build_object(
      'market', 'global',
      'notes', 'Default USD pricebook for Pro outside India'
    )
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b002',
    'BUSINESS_USD_GLOBAL_DEFAULT',
    NULL,
    'USD',
    'default',
    'business',
    1.0,
    true,
    '2026-03-31 00:00:00+00',
    jsonb_build_object(
      'market', 'global',
      'notes', 'Default USD pricebook for Business outside India'
    )
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b003',
    'PRO_INR_INDIA_DEFAULT',
    'IN',
    'INR',
    'default',
    'pro',
    1.0,
    true,
    '2026-03-31 00:00:00+00',
    jsonb_build_object(
      'market', 'india',
      'notes', 'Localized INR pricebook for Pro in India'
    )
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b004',
    'BUSINESS_INR_INDIA_DEFAULT',
    'IN',
    'INR',
    'default',
    'business',
    1.0,
    true,
    '2026-03-31 00:00:00+00',
    jsonb_build_object(
      'market', 'india',
      'notes', 'Localized INR pricebook for Business in India'
    )
  )
ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name,
  country_code = EXCLUDED.country_code,
  currency = EXCLUDED.currency,
  channel = EXCLUDED.channel,
  tier_code = EXCLUDED.tier_code,
  multiplier = EXCLUDED.multiplier,
  is_active = EXCLUDED.is_active,
  effective_from = EXCLUDED.effective_from,
  metadata_json = EXCLUDED.metadata_json;


-- ---------------------------------------------------------
-- 5) SKU sell prices by pricebook
-- Safer launch pricing:
--   Pro USD:       Talk 4.99/min, Cinematic 12.99/min
--   Business USD:  Talk 3.49/min, Cinematic 9.99/min
--   Pro INR:       Talk 399/min,  Cinematic 999/min
--   Business INR:  Talk 299/min,  Cinematic 799/min
-- ---------------------------------------------------------
INSERT INTO pricing_sku_prices (
  pricebook_id,
  sku_code,
  unit_money_override,
  metadata_json
)
VALUES
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b001',
    'LONGFORM_TALK_MIN',
    4.99,
    jsonb_build_object('currency_presentment', 'USD', 'tier_code', 'pro')
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b001',
    'LONGFORM_CINEMATIC_MIN',
    12.99,
    jsonb_build_object('currency_presentment', 'USD', 'tier_code', 'pro')
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b002',
    'LONGFORM_TALK_MIN',
    3.49,
    jsonb_build_object('currency_presentment', 'USD', 'tier_code', 'business')
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b002',
    'LONGFORM_CINEMATIC_MIN',
    9.99,
    jsonb_build_object('currency_presentment', 'USD', 'tier_code', 'business')
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b003',
    'LONGFORM_TALK_MIN',
    399.00,
    jsonb_build_object('currency_presentment', 'INR', 'tier_code', 'pro')
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b003',
    'LONGFORM_CINEMATIC_MIN',
    999.00,
    jsonb_build_object('currency_presentment', 'INR', 'tier_code', 'pro')
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b004',
    'LONGFORM_TALK_MIN',
    299.00,
    jsonb_build_object('currency_presentment', 'INR', 'tier_code', 'business')
  ),
  (
    '2d3b6dd8-3a44-4a5a-b4a4-ec6f4ad0b004',
    'LONGFORM_CINEMATIC_MIN',
    799.00,
    jsonb_build_object('currency_presentment', 'INR', 'tier_code', 'business')
  )
ON CONFLICT (pricebook_id, sku_code) DO UPDATE SET
  unit_money_override = EXCLUDED.unit_money_override,
  metadata_json = EXCLUDED.metadata_json;


-- ---------------------------------------------------------
-- 6) Monthly tier prices in USD / INR
-- country_code: '' for global/default, 'IN' for India
-- ---------------------------------------------------------
INSERT INTO pricing_tier_prices (
  tier_code,
  currency,
  country_code,
  monthly_price,
  is_active,
  metadata_json
)
VALUES
  (
    'pro',
    'USD',
    '',
    29.00,
    true,
    jsonb_build_object('market', 'global', 'notes', 'Global monthly price for Pro')
  ),
  (
    'business',
    'USD',
    '',
    99.00,
    true,
    jsonb_build_object('market', 'global', 'notes', 'Global monthly price for Business')
  ),
  (
    'pro',
    'INR',
    'IN',
    2499.00,
    true,
    jsonb_build_object('market', 'india', 'notes', 'India monthly price for Pro')
  ),
  (
    'business',
    'INR',
    'IN',
    8299.00,
    true,
    jsonb_build_object('market', 'india', 'notes', 'India monthly price for Business')
  )
ON CONFLICT (tier_code, currency, country_code) DO UPDATE SET
  monthly_price = EXCLUDED.monthly_price,
  is_active = EXCLUDED.is_active,
  metadata_json = EXCLUDED.metadata_json;


-- ---------------------------------------------------------
-- 7) FX reference rows for finance/reporting
-- Not used for customer-facing runtime pricing.
-- ---------------------------------------------------------
INSERT INTO pricing_fx_rates (
  base_currency,
  quote_currency,
  rate,
  as_of
)
VALUES
  ('USD', 'INR', 90.95940000, '2026-03-31 00:00:00+00'),
  ('INR', 'USD', 0.01099480, '2026-03-31 00:00:00+00')
ON CONFLICT (base_currency, quote_currency, as_of) DO UPDATE SET
  rate = EXCLUDED.rate;

COMMIT;
