-- 2026_05_22_desifaces_video_pricing_sustainable_migration_v2.sql
-- Purpose:
--   Reprice Fusion Extension / Longform video SKUs so Talking Video and Cinematic Video
--   are competitive but no longer priced below likely vendor COGS.
--
-- Fix in v2:
--   PostgreSQL does not allow referencing the target table alias inside a JOIN ON clause
--   within UPDATE ... FROM. The price update uses comma-style FROM and moves all target-table
--   references into WHERE.
--
-- Run from VM host:
--   docker exec -i desifaces-db psql -U desifaces_admin -d desifaces < migrations/2026_05_22_desifaces_video_pricing_sustainable_migration_v2.sql

\set ON_ERROR_STOP on
\pset pager off

BEGIN;

-- 1) Back up current rows once.
CREATE TABLE IF NOT EXISTS public.pricing_sku_prices_video_bak_20260522 AS
SELECT
  now() AS backed_up_at,
  sp.*
FROM public.pricing_sku_prices sp
WHERE sp.sku_code IN (
  'LONGFORM_TALK_ECONOMY_10S',
  'LONGFORM_TALK_ECONOMY_20S',
  'LONGFORM_TALK_ECONOMY_30S',
  'LONGFORM_TALK_PREMIUM_10S',
  'LONGFORM_TALK_PREMIUM_20S',
  'LONGFORM_TALK_PREMIUM_30S',
  'LONGFORM_CINEMATIC_MIN'
);

CREATE TABLE IF NOT EXISTS public.pricing_skus_video_bak_20260522 AS
SELECT
  now() AS backed_up_at,
  s.*
FROM public.pricing_skus s
WHERE s.code IN (
  'LONGFORM_TALK_ECONOMY_10S',
  'LONGFORM_TALK_ECONOMY_20S',
  'LONGFORM_TALK_ECONOMY_30S',
  'LONGFORM_TALK_PREMIUM_10S',
  'LONGFORM_TALK_PREMIUM_20S',
  'LONGFORM_TALK_PREMIUM_30S',
  'LONGFORM_CINEMATIC_MIN'
);

-- 2) Define target pricing in a temp table.
CREATE TEMP TABLE tmp_video_pricing_targets (
  sku_code text PRIMARY KEY,
  usd_default_money numeric(18,8) NOT NULL,
  usd_default_credits bigint NOT NULL,
  usd_mobile_money numeric(18,8) NOT NULL,
  usd_mobile_credits bigint NOT NULL,
  inr_default_money numeric(18,8) NOT NULL,
  inr_default_credits bigint NOT NULL,
  inr_mobile_money numeric(18,8) NOT NULL,
  inr_mobile_credits bigint NOT NULL,
  sku_default_credits bigint NOT NULL
) ON COMMIT DROP;

INSERT INTO tmp_video_pricing_targets (
  sku_code,
  usd_default_money, usd_default_credits,
  usd_mobile_money, usd_mobile_credits,
  inr_default_money, inr_default_credits,
  inr_mobile_money, inr_mobile_credits,
  sku_default_credits
)
VALUES
  ('LONGFORM_TALK_ECONOMY_10S',  1.99,  199,  2.49,  249,   149,   149,   199,   199,  199),
  ('LONGFORM_TALK_ECONOMY_20S',  3.49,  349,  4.49,  449,   249,   249,   299,   299,  349),
  ('LONGFORM_TALK_ECONOMY_30S',  4.99,  499,  5.99,  599,   399,   399,   499,   499,  499),

  ('LONGFORM_TALK_PREMIUM_10S',  4.99,  499,  5.99,  599,   349,   349,   449,   449,  499),
  ('LONGFORM_TALK_PREMIUM_20S',  8.99,  899,  9.99,  999,   649,   649,   799,   799,  899),
  ('LONGFORM_TALK_PREMIUM_30S', 12.99, 1299, 14.99, 1499,   999,   999,  1199,  1199, 1299),

  ('LONGFORM_CINEMATIC_MIN',    17.99, 1799, 19.99, 1999,  1499,  1499,  1799,  1799, 1799);

-- 3) Safety checks before updates.
DO $$
DECLARE
  missing_skus text;
  missing_prices text;
BEGIN
  SELECT string_agg(t.sku_code, ', ' ORDER BY t.sku_code)
  INTO missing_skus
  FROM tmp_video_pricing_targets t
  LEFT JOIN public.pricing_skus s ON s.code = t.sku_code
  WHERE s.code IS NULL;

  IF missing_skus IS NOT NULL THEN
    RAISE EXCEPTION 'Missing pricing_skus rows for: %', missing_skus;
  END IF;

  SELECT string_agg(t.sku_code, ', ' ORDER BY t.sku_code)
  INTO missing_prices
  FROM tmp_video_pricing_targets t
  WHERE NOT EXISTS (
    SELECT 1
    FROM public.pricing_sku_prices sp
    JOIN public.pricing_pricebooks pb ON pb.id = sp.pricebook_id
    WHERE sp.sku_code = t.sku_code
      AND pb.is_active = true
      AND pb.currency IN ('USD', 'INR')
  );

  IF missing_prices IS NOT NULL THEN
    RAISE EXCEPTION 'Missing active USD/INR pricing_sku_prices rows for: %', missing_prices;
  END IF;
END $$;

-- 4) Update SKU default credits and mark policy metadata.
UPDATE public.pricing_skus s
SET
  default_unit_credits = t.sku_default_credits,
  metadata_json =
    COALESCE(s.metadata_json, '{}'::jsonb)
    || jsonb_build_object(
      'pricing_seed', '20260522_sustainable_video_pricing_v1',
      'pricing_policy', 'sustainable_competitive_launch',
      'pricing_note', 'Video SKUs repriced to protect vendor COGS while remaining competitive across USD/INR and web/mobile channels.',
      'updated_at', now()::text
    )
FROM tmp_video_pricing_targets t
WHERE s.code = t.sku_code;

-- 5) Update all active USD/INR pricebook overrides.
-- IMPORTANT: Use comma-style FROM because PostgreSQL cannot reference target table alias
-- "sp" inside JOIN ON in UPDATE ... FROM.
UPDATE public.pricing_sku_prices sp
SET
  unit_money_override =
    CASE
      WHEN pb.currency = 'USD' AND pb.channel = 'mobile' THEN t.usd_mobile_money
      WHEN pb.currency = 'USD' THEN t.usd_default_money
      WHEN pb.currency = 'INR' AND pb.channel = 'mobile' THEN t.inr_mobile_money
      WHEN pb.currency = 'INR' THEN t.inr_default_money
      ELSE sp.unit_money_override
    END,
  unit_credits_override =
    CASE
      WHEN pb.currency = 'USD' AND pb.channel = 'mobile' THEN t.usd_mobile_credits
      WHEN pb.currency = 'USD' THEN t.usd_default_credits
      WHEN pb.currency = 'INR' AND pb.channel = 'mobile' THEN t.inr_mobile_credits
      WHEN pb.currency = 'INR' THEN t.inr_default_credits
      ELSE sp.unit_credits_override
    END,
  metadata_json =
    COALESCE(sp.metadata_json, '{}'::jsonb)
    || jsonb_build_object(
      'pricing_seed', '20260522_sustainable_video_pricing_v1',
      'pricing_policy', 'sustainable_competitive_launch',
      'mobile_price_includes_store_fee_buffer', CASE WHEN pb.channel = 'mobile' THEN true ELSE false END,
      'updated_at', now()::text
    )
FROM public.pricing_pricebooks pb, tmp_video_pricing_targets t
WHERE pb.id = sp.pricebook_id
  AND t.sku_code = sp.sku_code
  AND pb.is_active = true
  AND pb.currency IN ('USD', 'INR');

-- 6) Post-update safety check: ensure every active USD/INR video price row now matches target.
DO $$
DECLARE
  bad_count integer;
BEGIN
  SELECT count(*)
  INTO bad_count
  FROM public.pricing_sku_prices sp
  JOIN public.pricing_pricebooks pb ON pb.id = sp.pricebook_id
  JOIN tmp_video_pricing_targets t ON t.sku_code = sp.sku_code
  WHERE pb.is_active = true
    AND pb.currency IN ('USD', 'INR')
    AND (
      round(sp.unit_money_override, 2) <>
        round(
          CASE
            WHEN pb.currency = 'USD' AND pb.channel = 'mobile' THEN t.usd_mobile_money
            WHEN pb.currency = 'USD' THEN t.usd_default_money
            WHEN pb.currency = 'INR' AND pb.channel = 'mobile' THEN t.inr_mobile_money
            WHEN pb.currency = 'INR' THEN t.inr_default_money
          END,
          2
        )
      OR sp.unit_credits_override <>
        CASE
          WHEN pb.currency = 'USD' AND pb.channel = 'mobile' THEN t.usd_mobile_credits
          WHEN pb.currency = 'USD' THEN t.usd_default_credits
          WHEN pb.currency = 'INR' AND pb.channel = 'mobile' THEN t.inr_mobile_credits
          WHEN pb.currency = 'INR' THEN t.inr_default_credits
        END
    );

  IF bad_count > 0 THEN
    RAISE EXCEPTION 'Video pricing migration verification failed: % rows do not match target pricing.', bad_count;
  END IF;
END $$;

-- 7) Show final result.
SELECT
  pb.name AS pricebook,
  COALESCE(NULLIF(pb.country_code, ''), 'GLOBAL') AS country,
  pb.currency,
  pb.channel,
  COALESCE(NULLIF(pb.tier_code, ''), 'ANY') AS tier,
  sp.sku_code,
  sp.unit_credits_override AS credits,
  round(sp.unit_money_override, 2) AS money
FROM public.pricing_sku_prices sp
JOIN public.pricing_pricebooks pb
  ON pb.id = sp.pricebook_id
WHERE sp.sku_code IN (
  'LONGFORM_TALK_ECONOMY_10S',
  'LONGFORM_TALK_ECONOMY_20S',
  'LONGFORM_TALK_ECONOMY_30S',
  'LONGFORM_TALK_PREMIUM_10S',
  'LONGFORM_TALK_PREMIUM_20S',
  'LONGFORM_TALK_PREMIUM_30S',
  'LONGFORM_CINEMATIC_MIN'
)
ORDER BY
  sp.sku_code,
  pb.currency,
  pb.channel,
  pb.tier_code NULLS FIRST,
  pb.name;

COMMIT;
