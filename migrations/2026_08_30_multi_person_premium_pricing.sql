-- desifaces V3: multi-person premium pricing
--
-- Architecture contract:
--   * exactly one multi-person SKU/variant per studio
--   * participant count is metadata, never SKU identity
--   * single-person pricing is unchanged
--   * natural workload remains the billing quantity
--       Face   -> num_edits (requested variants/runs)
--       Audio  -> chars_1k
--       Fusion -> minutes
--   * multi-person premium unit rate = corresponding baseline * 1.25
--
-- No subscription, entitlement, credit-pack, Stripe, Apple, or Google product
-- records are changed by this migration.

BEGIN;

CREATE TEMP TABLE _multi_person_pricing_map (
  target_code text PRIMARY KEY,
  source_code text NOT NULL,
  display_name text NOT NULL,
  category text NOT NULL,
  qty_param text NOT NULL,
  premium_rate_multiplier numeric(10,6) NOT NULL
) ON COMMIT DROP;

INSERT INTO _multi_person_pricing_map
  (target_code, source_code, display_name, category, qty_param, premium_rate_multiplier)
VALUES
  ('FACE_MULTI_PERSON',   'FACE_EDIT_PREMIUM_RUN', 'Face Studio - Multi-Person Premium',   'face',   'num_edits', 1.25),
  ('AUDIO_MULTI_PERSON',  'AUDIO_TTS_1K_CHARS',    'Audio Studio - Multi-Person Premium',  'audio',  'chars_1k',  1.25),
  ('FUSION_MULTI_PERSON', 'FUSION_TALK_MIN',       'Fusion Studio - Multi-Person Premium', 'fusion', 'minutes',   1.25);

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

INSERT INTO pricing_skus (
  code, name, unit, category, provider_hint, default_unit_credits,
  status, effective_from, effective_to, metadata_json
)
SELECT
  m.target_code,
  m.display_name,
  src.unit,
  m.category,
  src.provider_hint,
  CEIL(src.default_unit_credits * m.premium_rate_multiplier)::bigint,
  'active',
  now(),
  NULL,
  COALESCE(src.metadata_json, '{}'::jsonb) || jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'minimum_participants', 2,
    'participant_count_in_sku', false,
    'pricing_policy', 'multi_person_workload_v1',
    'quantity_param', m.qty_param,
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
  metadata_json = EXCLUDED.metadata_json;

INSERT INTO pricing_sku_prices (
  pricebook_id, sku_code, unit_credits_override, unit_money_override,
  min_qty, max_qty, metadata_json
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
  1,
  NULL,
  COALESCE(p.metadata_json, '{}'::jsonb) || jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count_in_sku', false,
    'pricing_policy', 'multi_person_workload_v1',
    'quantity_param', m.qty_param,
    'premium_rate_multiplier', m.premium_rate_multiplier,
    'source_sku', m.source_code
  )
FROM _multi_person_pricing_map m
JOIN pricing_sku_prices p ON p.sku_code = m.source_code
ON CONFLICT (pricebook_id, sku_code) DO UPDATE SET
  unit_credits_override = EXCLUDED.unit_credits_override,
  unit_money_override = EXCLUDED.unit_money_override,
  min_qty = 1,
  max_qty = NULL,
  metadata_json = EXCLUDED.metadata_json;

INSERT INTO pricing_variants (
  code, name, category, is_active, metadata_json
)
SELECT
  m.target_code,
  m.display_name,
  m.category,
  true,
  jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'minimum_participants', 2,
    'participant_count_in_sku', false,
    'pricing_policy', 'multi_person_workload_v1',
    'qty_param', m.qty_param,
    'catalog_rule', 'one_variant_per_studio'
  )
FROM _multi_person_pricing_map m
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  category = EXCLUDED.category,
  is_active = true,
  metadata_json = EXCLUDED.metadata_json;

DELETE FROM pricing_variant_lines
WHERE variant_code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON');

INSERT INTO pricing_variant_lines (
  variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json
)
SELECT
  m.target_code,
  m.target_code,
  'param',
  NULL,
  m.qty_param,
  jsonb_build_object(
    'multi_person', true,
    'participant_count_in_sku', false,
    'pricing_policy', 'multi_person_workload_v1',
    'source_sku', m.source_code
  )
FROM _multi_person_pricing_map m;

DO $$
DECLARE
  sku_count integer;
  variant_count integer;
  line_count integer;
  bad_contract_count integer;
  bad_credit_price_count integer;
  bad_money_price_count integer;
  bad_bounds_count integer;
BEGIN
  SELECT count(*) INTO sku_count
  FROM pricing_skus
  WHERE code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON');

  SELECT count(*) INTO variant_count
  FROM pricing_variants
  WHERE code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON');

  SELECT count(*) INTO line_count
  FROM pricing_variant_lines l
  JOIN _multi_person_pricing_map m ON m.target_code = l.variant_code
  WHERE l.sku_code = m.target_code
    AND l.qty_mode = 'param'
    AND l.qty_param = m.qty_param;

  SELECT count(*) INTO bad_contract_count
  FROM _multi_person_pricing_map m
  JOIN pricing_skus s ON s.code = m.target_code
  JOIN pricing_variants v ON v.code = m.target_code
  WHERE COALESCE((s.metadata_json ->> 'participant_count_in_sku')::boolean, true) <> false
     OR s.metadata_json ->> 'quantity_param' IS DISTINCT FROM m.qty_param
     OR v.metadata_json ->> 'qty_param' IS DISTINCT FROM m.qty_param;

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

  SELECT count(*) INTO bad_bounds_count
  FROM pricing_sku_prices
  WHERE sku_code IN ('FACE_MULTI_PERSON', 'AUDIO_MULTI_PERSON', 'FUSION_MULTI_PERSON')
    AND (min_qty IS DISTINCT FROM 1 OR max_qty IS NOT NULL);

  IF sku_count <> 3 THEN
    RAISE EXCEPTION 'Expected exactly 3 multi-person SKUs, found %', sku_count;
  END IF;
  IF variant_count <> 3 THEN
    RAISE EXCEPTION 'Expected exactly 3 multi-person variants, found %', variant_count;
  END IF;
  IF line_count <> 3 THEN
    RAISE EXCEPTION 'Expected exactly 3 native-quantity multi-person variant lines, found %', line_count;
  END IF;
  IF bad_contract_count <> 0 THEN
    RAISE EXCEPTION 'Multi-person SKU/variant contract mismatch count=%', bad_contract_count;
  END IF;
  IF bad_credit_price_count <> 0 THEN
    RAISE EXCEPTION 'Multi-person credit price mismatch count=%', bad_credit_price_count;
  END IF;
  IF bad_money_price_count <> 0 THEN
    RAISE EXCEPTION 'Multi-person money price mismatch count=%', bad_money_price_count;
  END IF;
  IF bad_bounds_count <> 0 THEN
    RAISE EXCEPTION 'Multi-person price rows must be min_qty=1/max_qty=NULL; mismatch count=%', bad_bounds_count;
  END IF;
END $$;

COMMIT;
