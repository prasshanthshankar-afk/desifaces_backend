-- desifaces V3: simplified multi-person premium pricing
--
-- Commercial/architecture rule:
--   * Single-person flows keep their existing pricing unchanged.
--   * Every 2+ person flow uses ONE domain-level multi-person pricing entry point.
--   * Participant count is pricing metadata/input, NEVER part of the SKU code.
--   * Workload scales through the caller-supplied `premium_units` quantity.
--   * The same catalog supports 2, 3, 4, 5 ... participants without new SKUs.
--
-- New customer-pricing entry points (and only these three):
--   FACE_MULTI_PERSON
--   AUDIO_MULTI_PERSON
--   FUSION_MULTI_PERSON
--
-- V1 premium unit-rate policy:
--   multi-person unit rate = corresponding baseline unit rate * 1.25
--   total charge then scales with `premium_units` supplied by orchestration.
--
-- This migration intentionally DOES NOT modify:
--   subscriptions, entitlements, credit packs, payment products,
--   Apple/Google/Stripe configuration, or any single-person SKU/price.

BEGIN;

CREATE TEMP TABLE _multi_person_pricing_map (
  target_code text PRIMARY KEY,
  source_code text NOT NULL,
  display_name text NOT NULL,
  target_unit text NOT NULL,
  category text NOT NULL,
  premium_rate_multiplier numeric(10,6) NOT NULL
) ON COMMIT DROP;

INSERT INTO _multi_person_pricing_map
  (target_code, source_code, display_name, target_unit, category, premium_rate_multiplier)
VALUES
  ('FACE_MULTI_PERSON',   'FACE_EDIT_PREMIUM_RUN', 'Face Studio - Multi-Person Premium',   'premium_unit', 'face',   1.25),
  ('AUDIO_MULTI_PERSON',  'AUDIO_TTS_1K_CHARS',    'Audio Studio - Multi-Person Premium',  'premium_unit', 'audio',  1.25),
  ('FUSION_MULTI_PERSON', 'FUSION_TALK_MIN',       'Fusion Studio - Multi-Person Premium', 'premium_unit', 'fusion', 1.25);

-- Fail closed if the baseline catalog required to derive V1 rates is incomplete.
DO $$
DECLARE
  missing_sources text;
BEGIN
  SELECT string_agg(m.source_code, ', ' ORDER BY m.source_code)
    INTO missing_sources
  FROM _multi_person_pricing_map m
  LEFT JOIN pricing_skus s ON s.code = m.source_code
  WHERE s.code IS NULL;

  IF missing_sources IS NOT NULL THEN
    RAISE EXCEPTION 'Multi-person pricing migration aborted; missing baseline SKU(s): %', missing_sources;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 1. Exactly three generic multi-person SKUs.
--    No MP2 / MP3 / MP4 / MP5 SKU proliferation.
-- ---------------------------------------------------------------------------
INSERT INTO pricing_skus (
  code,
  name,
  unit,
  category,
  provider_hint,
  default_unit_credits,
  status,
  effective_from,
  effective_to,
  metadata_json
)
SELECT
  m.target_code,
  m.display_name,
  m.target_unit,
  m.category,
  src.provider_hint,
  CEIL(src.default_unit_credits * m.premium_rate_multiplier)::bigint,
  'active',
  now(),
  NULL,
  jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count_in_sku', false,
    'minimum_participants', 2,
    'pricing_policy', 'multi_person_workload_v1',
    'billing_quantity', 'caller_computed_premium_units',
    'quantity_param', 'premium_units',
    'premium_rate_multiplier', m.premium_rate_multiplier,
    'source_sku', m.source_code,
    'catalog_rule', 'one_sku_per_studio'
  )
FROM _multi_person_pricing_map m
JOIN pricing_skus src ON src.code = m.source_code
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  unit = EXCLUDED.unit,
  category = EXCLUDED.category,
  provider_hint = EXCLUDED.provider_hint,
  default_unit_credits = EXCLUDED.default_unit_credits,
  status = 'active',
  effective_to = NULL,
  metadata_json = pricing_skus.metadata_json || EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- 2. Clone every applicable pricebook row from the baseline SKU and apply the
--    same 1.25 premium unit-rate uplift. Quantity remains completely separate.
-- ---------------------------------------------------------------------------
INSERT INTO pricing_sku_prices (
  pricebook_id,
  sku_code,
  unit_credits_override,
  unit_money_override,
  min_qty,
  max_qty,
  metadata_json
)
SELECT
  p.pricebook_id,
  m.target_code,
  CASE
    WHEN p.unit_credits_override IS NULL THEN NULL
    ELSE CEIL(p.unit_credits_override * m.premium_rate_multiplier)::bigint
  END,
  CASE
    WHEN p.unit_money_override IS NULL THEN NULL
    ELSE ROUND(p.unit_money_override * m.premium_rate_multiplier, 8)
  END,
  p.min_qty,
  p.max_qty,
  COALESCE(p.metadata_json, '{}'::jsonb) || jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count_in_sku', false,
    'pricing_policy', 'multi_person_workload_v1',
    'quantity_param', 'premium_units',
    'premium_rate_multiplier', m.premium_rate_multiplier,
    'source_sku', m.source_code
  )
FROM _multi_person_pricing_map m
JOIN pricing_sku_prices p ON p.sku_code = m.source_code
ON CONFLICT (pricebook_id, sku_code) DO UPDATE SET
  unit_credits_override = EXCLUDED.unit_credits_override,
  unit_money_override = EXCLUDED.unit_money_override,
  min_qty = EXCLUDED.min_qty,
  max_qty = EXCLUDED.max_qty,
  metadata_json = pricing_sku_prices.metadata_json || EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- 3. Matching variant codes keep preview/quote integration simple.
--    Variant code == SKU code intentionally. They are separate catalog tables.
-- ---------------------------------------------------------------------------
INSERT INTO pricing_variants (
  code,
  name,
  category,
  is_active,
  metadata_json
)
SELECT
  m.target_code,
  m.display_name,
  m.category,
  true,
  jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count_in_sku', false,
    'minimum_participants', 2,
    'pricing_policy', 'multi_person_workload_v1',
    'quantity_param', 'premium_units',
    'catalog_rule', 'one_variant_per_studio'
  )
FROM _multi_person_pricing_map m
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  category = EXCLUDED.category,
  is_active = true,
  metadata_json = pricing_variants.metadata_json || EXCLUDED.metadata_json;

-- Keep one deterministic line per multi-person variant. This migration owns
-- these new variant codes, so replacing their lines is safe and idempotent.
DELETE FROM pricing_variant_lines
WHERE variant_code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON');

INSERT INTO pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_value,
  qty_param,
  metadata_json
)
SELECT
  m.target_code,
  m.target_code,
  'param',
  NULL,
  'premium_units',
  jsonb_build_object(
    'multi_person', true,
    'participant_count_in_sku', false,
    'pricing_policy', 'multi_person_workload_v1'
  )
FROM _multi_person_pricing_map m;

-- ---------------------------------------------------------------------------
-- 4. Certification gates: fail the transaction rather than leave a partial or
--    proliferated catalog.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  sku_count integer;
  variant_count integer;
  line_count integer;
  bad_catalog_count integer;
  bad_credit_price_count integer;
  bad_money_price_count integer;
BEGIN
  SELECT count(*) INTO sku_count
  FROM pricing_skus
  WHERE code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON');

  SELECT count(*) INTO variant_count
  FROM pricing_variants
  WHERE code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON');

  SELECT count(*) INTO line_count
  FROM pricing_variant_lines
  WHERE variant_code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON')
    AND sku_code = variant_code
    AND qty_mode = 'param'
    AND qty_param = 'premium_units';

  SELECT count(*) INTO bad_catalog_count
  FROM pricing_skus
  WHERE code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON')
    AND (
      COALESCE((metadata_json ->> 'participant_count_in_sku')::boolean, true) <> false
      OR metadata_json ->> 'quantity_param' <> 'premium_units'
    );

  SELECT count(*) INTO bad_credit_price_count
  FROM _multi_person_pricing_map m
  JOIN pricing_sku_prices src_price ON src_price.sku_code = m.source_code
  JOIN pricing_sku_prices mp_price
    ON mp_price.pricebook_id = src_price.pricebook_id
   AND mp_price.sku_code = m.target_code
  WHERE src_price.unit_credits_override IS NOT NULL
    AND mp_price.unit_credits_override IS DISTINCT FROM
        CEIL(src_price.unit_credits_override * m.premium_rate_multiplier)::bigint;

  SELECT count(*) INTO bad_money_price_count
  FROM _multi_person_pricing_map m
  JOIN pricing_sku_prices src_price ON src_price.sku_code = m.source_code
  JOIN pricing_sku_prices mp_price
    ON mp_price.pricebook_id = src_price.pricebook_id
   AND mp_price.sku_code = m.target_code
  WHERE src_price.unit_money_override IS NOT NULL
    AND mp_price.unit_money_override IS DISTINCT FROM
        ROUND(src_price.unit_money_override * m.premium_rate_multiplier, 8);

  IF sku_count <> 3 THEN
    RAISE EXCEPTION 'Expected exactly 3 multi-person SKUs, found %', sku_count;
  END IF;

  IF variant_count <> 3 THEN
    RAISE EXCEPTION 'Expected exactly 3 multi-person variants, found %', variant_count;
  END IF;

  IF line_count <> 3 THEN
    RAISE EXCEPTION 'Expected exactly 3 multi-person variant lines, found %', line_count;
  END IF;

  IF bad_catalog_count <> 0 THEN
    RAISE EXCEPTION 'Multi-person catalog metadata/quantity contract invalid for % SKU(s)', bad_catalog_count;
  END IF;

  IF bad_credit_price_count <> 0 THEN
    RAISE EXCEPTION 'Multi-person credit price certification failed for % pricebook row(s)', bad_credit_price_count;
  END IF;

  IF bad_money_price_count <> 0 THEN
    RAISE EXCEPTION 'Multi-person money price certification failed for % pricebook row(s)', bad_money_price_count;
  END IF;
END $$;

COMMIT;
