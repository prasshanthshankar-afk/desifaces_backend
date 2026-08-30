-- desifaces V3: Multi-Person Premium Pricing v1
-- Date: 2026-08-30
--
-- Scope:
--   * Adds dedicated 2-person and 3-person premium SKUs for Face, Audio and Fusion.
--   * Adds matching pricing variants so callers can stay on the existing
--     quote -> reserve -> finalize pricing lifecycle.
--   * Clones every existing pricebook override from the corresponding
--     single-person SKU, applying the multi-person premium multiplier.
--   * Does NOT change subscription entitlements, payment products, credit packs,
--     or any existing single-person SKU/variant.
--
-- Commercial policy v1:
--   * 2-person: 1.75x the corresponding single-person unit price/credits.
--   * 3-person: 2.50x the corresponding single-person unit price/credits.
--
-- The premium reflects multi-character orchestration, multiple face/audio
-- pipelines, synchronization/composition overhead and additional revision load.
-- Customer pricing remains driven by svc-pricing; provider COGS remains separate.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) Source -> premium SKU map
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE tmp_multi_person_sku_map (
  source_code       text NOT NULL,
  target_code       text PRIMARY KEY,
  participant_count integer NOT NULL CHECK (participant_count IN (2, 3)),
  multiplier        numeric(10,4) NOT NULL,
  target_name       text NOT NULL
) ON COMMIT DROP;

INSERT INTO tmp_multi_person_sku_map
  (source_code, target_code, participant_count, multiplier, target_name)
VALUES
  -- Face Studio
  ('IMG_STD_RUN',           'IMG_STD_RUN_MP2',           2, 1.7500, 'Image Generation Standard - Multi-Person 2'),
  ('IMG_STD_RUN',           'IMG_STD_RUN_MP3',           3, 2.5000, 'Image Generation Standard - Multi-Person 3'),
  ('IMG_HD_RUN',            'IMG_HD_RUN_MP2',            2, 1.7500, 'Image Generation HD - Multi-Person 2'),
  ('IMG_HD_RUN',            'IMG_HD_RUN_MP3',            3, 2.5000, 'Image Generation HD - Multi-Person 3'),
  ('FACE_EDIT_PREMIUM_RUN', 'FACE_EDIT_PREMIUM_RUN_MP2', 2, 1.7500, 'Premium Face Edit / Identity-lock - Multi-Person 2'),
  ('FACE_EDIT_PREMIUM_RUN', 'FACE_EDIT_PREMIUM_RUN_MP3', 3, 2.5000, 'Premium Face Edit / Identity-lock - Multi-Person 3'),

  -- Audio Studio
  ('AUDIO_TTS_1K_CHARS',    'AUDIO_TTS_1K_CHARS_MP2',    2, 1.7500, 'TTS - Multi-Person 2 (per 1k characters)'),
  ('AUDIO_TTS_1K_CHARS',    'AUDIO_TTS_1K_CHARS_MP3',    3, 2.5000, 'TTS - Multi-Person 3 (per 1k characters)'),

  -- Fusion Studio
  ('FUSION_TALK_MIN',       'FUSION_TALK_MIN_MP2',       2, 1.7500, 'Talking Video - Multi-Person 2 (per minute)'),
  ('FUSION_TALK_MIN',       'FUSION_TALK_MIN_MP3',       3, 2.5000, 'Talking Video - Multi-Person 3 (per minute)');

-- Fail closed if the expected single-person source catalog is not present.
DO $$
DECLARE
  missing_sources text;
BEGIN
  SELECT string_agg(DISTINCT m.source_code, ', ' ORDER BY m.source_code)
    INTO missing_sources
  FROM tmp_multi_person_sku_map m
  LEFT JOIN pricing_skus s ON s.code = m.source_code
  WHERE s.code IS NULL;

  IF missing_sources IS NOT NULL THEN
    RAISE EXCEPTION 'Multi-person pricing migration blocked; missing source SKU(s): %', missing_sources;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2) Premium SKU catalog
-- ---------------------------------------------------------------------------
INSERT INTO pricing_skus
  (code, name, unit, category, provider_hint, default_unit_credits,
   status, effective_from, effective_to, metadata_json)
SELECT
  m.target_code,
  m.target_name,
  s.unit,
  s.category,
  s.provider_hint,
  CEIL(s.default_unit_credits * m.multiplier)::bigint,
  s.status,
  '2026-08-30 00:00:00+00'::timestamptz,
  s.effective_to,
  COALESCE(s.metadata_json, '{}'::jsonb) || jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count', m.participant_count,
    'pricing_multiplier', m.multiplier,
    'source_sku', m.source_code,
    'pricing_policy', 'multi_person_complexity_premium_v1',
    'pricing_seed', '20260830_multi_person_premium_v1'
  )
FROM tmp_multi_person_sku_map m
JOIN pricing_skus s ON s.code = m.source_code
ON CONFLICT (code) DO UPDATE
SET name                 = EXCLUDED.name,
    unit                 = EXCLUDED.unit,
    category             = EXCLUDED.category,
    provider_hint        = EXCLUDED.provider_hint,
    default_unit_credits = EXCLUDED.default_unit_credits,
    status               = EXCLUDED.status,
    effective_to         = EXCLUDED.effective_to,
    metadata_json        = EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- 3) Clone all existing web/mobile/API pricebook overrides
-- ---------------------------------------------------------------------------
-- This intentionally derives from the current source-SKU price in each pricebook.
-- It therefore preserves channel/currency policy (USD/INR, web/mobile/API) without
-- duplicating those business rules in application code.
INSERT INTO pricing_sku_prices
  (pricebook_id, sku_code, unit_credits_override, unit_money_override,
   min_qty, max_qty, metadata_json)
SELECT
  p.pricebook_id,
  m.target_code,
  CASE
    WHEN p.unit_credits_override IS NULL THEN NULL
    ELSE CEIL(p.unit_credits_override * m.multiplier)::bigint
  END,
  CASE
    WHEN p.unit_money_override IS NULL THEN NULL
    ELSE ROUND(p.unit_money_override * m.multiplier, 2)
  END,
  p.min_qty,
  p.max_qty,
  COALESCE(p.metadata_json, '{}'::jsonb) || jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count', m.participant_count,
    'pricing_multiplier', m.multiplier,
    'source_sku', m.source_code,
    'pricing_policy', 'multi_person_complexity_premium_v1',
    'pricing_seed', '20260830_multi_person_premium_v1'
  )
FROM tmp_multi_person_sku_map m
JOIN pricing_sku_prices p ON p.sku_code = m.source_code
ON CONFLICT (pricebook_id, sku_code) DO UPDATE
SET unit_credits_override = EXCLUDED.unit_credits_override,
    unit_money_override   = EXCLUDED.unit_money_override,
    min_qty               = EXCLUDED.min_qty,
    max_qty               = EXCLUDED.max_qty,
    metadata_json         = EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- 4) Source -> premium variant map
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE tmp_multi_person_variant_map (
  source_variant    text NOT NULL,
  target_variant    text PRIMARY KEY,
  participant_count integer NOT NULL CHECK (participant_count IN (2, 3)),
  multiplier        numeric(10,4) NOT NULL,
  target_name       text NOT NULL
) ON COMMIT DROP;

INSERT INTO tmp_multi_person_variant_map
  (source_variant, target_variant, participant_count, multiplier, target_name)
VALUES
  -- Face Studio
  ('FACE_IMAGE_STD_BATCH',    'FACE_IMAGE_STD_BATCH_MP2',    2, 1.7500, 'Face Studio: Standard images - Multi-Person 2'),
  ('FACE_IMAGE_STD_BATCH',    'FACE_IMAGE_STD_BATCH_MP3',    3, 2.5000, 'Face Studio: Standard images - Multi-Person 3'),
  ('FACE_IMAGE_HD_BATCH',     'FACE_IMAGE_HD_BATCH_MP2',     2, 1.7500, 'Face Studio: HD images - Multi-Person 2'),
  ('FACE_IMAGE_HD_BATCH',     'FACE_IMAGE_HD_BATCH_MP3',     3, 2.5000, 'Face Studio: HD images - Multi-Person 3'),
  ('FACE_EDIT_PREMIUM_BATCH', 'FACE_EDIT_PREMIUM_BATCH_MP2', 2, 1.7500, 'Face Studio: Premium edits - Multi-Person 2'),
  ('FACE_EDIT_PREMIUM_BATCH', 'FACE_EDIT_PREMIUM_BATCH_MP3', 3, 2.5000, 'Face Studio: Premium edits - Multi-Person 3'),

  -- Audio Studio
  ('AUDIO_TTS',               'AUDIO_TTS_MP2',               2, 1.7500, 'Audio Studio: TTS - Multi-Person 2'),
  ('AUDIO_TTS',               'AUDIO_TTS_MP3',               3, 2.5000, 'Audio Studio: TTS - Multi-Person 3'),

  -- Fusion Studio
  ('FUSION_TALKING_VIDEO',    'FUSION_TALKING_VIDEO_MP2',    2, 1.7500, 'Fusion Studio: Talking video - Multi-Person 2'),
  ('FUSION_TALKING_VIDEO',    'FUSION_TALKING_VIDEO_MP3',    3, 2.5000, 'Fusion Studio: Talking video - Multi-Person 3');

-- Fail closed if the corresponding single-person variants are unavailable.
DO $$
DECLARE
  missing_variants text;
BEGIN
  SELECT string_agg(DISTINCT m.source_variant, ', ' ORDER BY m.source_variant)
    INTO missing_variants
  FROM tmp_multi_person_variant_map m
  LEFT JOIN pricing_variants v ON v.code = m.source_variant
  WHERE v.code IS NULL;

  IF missing_variants IS NOT NULL THEN
    RAISE EXCEPTION 'Multi-person pricing migration blocked; missing source variant(s): %', missing_variants;
  END IF;
END $$;

INSERT INTO pricing_variants
  (code, name, category, is_active, metadata_json)
SELECT
  m.target_variant,
  m.target_name,
  v.category,
  v.is_active,
  COALESCE(v.metadata_json, '{}'::jsonb) || jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count', m.participant_count,
    'pricing_multiplier', m.multiplier,
    'source_variant', m.source_variant,
    'pricing_policy', 'multi_person_complexity_premium_v1',
    'pricing_seed', '20260830_multi_person_premium_v1'
  )
FROM tmp_multi_person_variant_map m
JOIN pricing_variants v ON v.code = m.source_variant
ON CONFLICT (code) DO UPDATE
SET name          = EXCLUDED.name,
    category      = EXCLUDED.category,
    is_active     = EXCLUDED.is_active,
    metadata_json = EXCLUDED.metadata_json;

-- Clone each source variant line while substituting the matching MP2/MP3 SKU.
INSERT INTO pricing_variant_lines
  (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
SELECT
  vm.target_variant,
  sm.target_code,
  line.qty_mode,
  line.qty_value,
  line.qty_param,
  COALESCE(line.metadata_json, '{}'::jsonb) || jsonb_build_object(
    'multi_person', true,
    'premium', true,
    'participant_count', vm.participant_count,
    'source_variant', vm.source_variant,
    'source_sku', sm.source_code,
    'pricing_policy', 'multi_person_complexity_premium_v1'
  )
FROM tmp_multi_person_variant_map vm
JOIN pricing_variant_lines line
  ON line.variant_code = vm.source_variant
JOIN tmp_multi_person_sku_map sm
  ON sm.source_code = line.sku_code
 AND sm.participant_count = vm.participant_count
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value     = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- ---------------------------------------------------------------------------
-- 5) Certification gates
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  sku_count integer;
  variant_count integer;
  variant_line_count integer;
  bad_credit_rows integer;
  bad_money_rows integer;
BEGIN
  SELECT count(*) INTO sku_count
  FROM pricing_skus s
  JOIN tmp_multi_person_sku_map m ON m.target_code = s.code;

  IF sku_count <> 10 THEN
    RAISE EXCEPTION 'Multi-person pricing certification failed: expected 10 premium SKUs, found %', sku_count;
  END IF;

  SELECT count(*) INTO variant_count
  FROM pricing_variants v
  JOIN tmp_multi_person_variant_map m ON m.target_variant = v.code;

  IF variant_count <> 10 THEN
    RAISE EXCEPTION 'Multi-person pricing certification failed: expected 10 premium variants, found %', variant_count;
  END IF;

  SELECT count(*) INTO variant_line_count
  FROM pricing_variant_lines l
  JOIN tmp_multi_person_variant_map m ON m.target_variant = l.variant_code;

  IF variant_line_count <> 10 THEN
    RAISE EXCEPTION 'Multi-person pricing certification failed: expected 10 premium variant lines, found %', variant_line_count;
  END IF;

  SELECT count(*) INTO bad_credit_rows
  FROM tmp_multi_person_sku_map m
  JOIN pricing_sku_prices src ON src.sku_code = m.source_code
  JOIN pricing_sku_prices dst
    ON dst.pricebook_id = src.pricebook_id
   AND dst.sku_code = m.target_code
  WHERE src.unit_credits_override IS NOT NULL
    AND dst.unit_credits_override <> CEIL(src.unit_credits_override * m.multiplier)::bigint;

  IF bad_credit_rows <> 0 THEN
    RAISE EXCEPTION 'Multi-person pricing certification failed: % pricebook credit override mismatch(es)', bad_credit_rows;
  END IF;

  SELECT count(*) INTO bad_money_rows
  FROM tmp_multi_person_sku_map m
  JOIN pricing_sku_prices src ON src.sku_code = m.source_code
  JOIN pricing_sku_prices dst
    ON dst.pricebook_id = src.pricebook_id
   AND dst.sku_code = m.target_code
  WHERE src.unit_money_override IS NOT NULL
    AND dst.unit_money_override <> ROUND(src.unit_money_override * m.multiplier, 2);

  IF bad_money_rows <> 0 THEN
    RAISE EXCEPTION 'Multi-person pricing certification failed: % pricebook money override mismatch(es)', bad_money_rows;
  END IF;
END $$;

COMMIT;
