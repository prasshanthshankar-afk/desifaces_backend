-- services/svc-pricing/app/app/migrations/0002_pricing_seed.sql
-- Seed data for svc-pricing (tiers, pricebooks, skus, variants, packs).
-- Idempotent: uses ON CONFLICT for PK-backed tables.

BEGIN;

-- ------------------------------------------------------------
-- 1) Tiers (GTM)
-- ------------------------------------------------------------
INSERT INTO pricing_tiers (code, name, monthly_grant_credits, is_active)
VALUES
  ('free',     'Free',     200,   true),
  ('creator',  'Creator',  3000,  true),
  ('pro',      'Pro',      8000,  true),
  ('business', 'Business', 25000, true),
  ('enterprise','Enterprise',0,   true),
  ('developer','Developer',0,     true)
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name,
    monthly_grant_credits = EXCLUDED.monthly_grant_credits,
    is_active = EXCLUDED.is_active;

-- ------------------------------------------------------------
-- 2) Tier prices (subscriptions/upgrades)
-- IMPORTANT: pricing_tier_prices.country_code is treated as '' for global, 'IN' for India
-- ------------------------------------------------------------
INSERT INTO pricing_tier_prices (tier_code, currency, country_code, monthly_price, is_active, metadata_json)
VALUES
  -- USD global
  ('free',     'USD', '', 0.00,  true, '{"note":"Free tier"}'),
  ('creator',  'USD', '', 19.00, true, '{"credits_included":3000}'),
  ('pro',      'USD', '', 39.00, true, '{"credits_included":8000}'),
  ('business', 'USD', '', 99.00, true, '{"credits_included":25000}'),

  -- INR India (market-competitive launch pricing)
  ('free',     'INR', 'IN', 0.00,   true, '{"note":"Free tier"}'),
  ('creator',  'INR', 'IN', 1499.00,true, '{"credits_included":3000}'),
  ('pro',      'INR', 'IN', 2999.00,true, '{"credits_included":8000}'),
  ('business', 'INR', 'IN', 8999.00,true, '{"credits_included":25000}')
ON CONFLICT (tier_code, currency, country_code) DO UPDATE
SET monthly_price = EXCLUDED.monthly_price,
    is_active     = EXCLUDED.is_active,
    metadata_json = EXCLUDED.metadata_json;

-- ------------------------------------------------------------
-- 3) Credit value + FX
-- Use a fixed effective_from/as_of so seeding is repeatable.
-- money_per_credit:
--   USD: 0.01 per credit  (1 credit = $0.01)
--   INR: 0.909594 per credit (derived from USD->INR ~ 90.9594)
-- ------------------------------------------------------------
INSERT INTO pricing_credit_value (currency, money_per_credit, rounding_mode, effective_from, effective_to)
VALUES
  ('USD', 0.01000000, 'ceil', '2026-03-05 00:00:00+00', NULL),
  ('INR', 0.90959400, 'ceil', '2026-03-05 00:00:00+00', NULL)
ON CONFLICT (currency, effective_from) DO UPDATE
SET money_per_credit = EXCLUDED.money_per_credit,
    rounding_mode    = EXCLUDED.rounding_mode,
    effective_to     = EXCLUDED.effective_to;

INSERT INTO pricing_fx_rates (base_currency, quote_currency, rate, as_of)
VALUES
  ('USD', 'INR', 90.95940000, '2026-03-05 00:00:00+00')
ON CONFLICT (base_currency, quote_currency, as_of) DO UPDATE
SET rate = EXCLUDED.rate;

-- ------------------------------------------------------------
-- 4) Pricebooks (web/mobile enabled now; api pre-seeded inactive)
-- Use fixed UUIDs so other seed rows can reference these deterministically.
-- ------------------------------------------------------------
INSERT INTO pricing_pricebooks
  (id, name, country_code, currency, channel, tier_code, multiplier, is_active, effective_from, effective_to, metadata_json)
VALUES
  ('11111111-1111-1111-1111-111111111111', 'GLOBAL_USD_WEB_V1',   '',   'USD', 'web',    NULL, 1.00, true,  '2026-03-05 00:00:00+00', NULL, '{"version":"v1"}'),
  ('22222222-2222-2222-2222-222222222222', 'GLOBAL_USD_MOBILE_V1','',   'USD', 'mobile', NULL, 1.00, true,  '2026-03-05 00:00:00+00', NULL, '{"version":"v1"}'),
  ('33333333-3333-3333-3333-333333333333', 'INDIA_INR_WEB_V1',    'IN', 'INR', 'web',    NULL, 1.00, true,  '2026-03-05 00:00:00+00', NULL, '{"version":"v1"}'),
  ('44444444-4444-4444-4444-444444444444', 'INDIA_INR_MOBILE_V1', 'IN', 'INR', 'mobile', NULL, 1.00, true,  '2026-03-05 00:00:00+00', NULL, '{"version":"v1"}'),

  -- API pricebooks: seeded now, can enable later (next ~3 months)
  ('55555555-5555-5555-5555-555555555555', 'GLOBAL_USD_API_V1',   '',   'USD', 'api',    NULL, 1.15, false, '2026-03-05 00:00:00+00', NULL, '{"version":"v1","note":"preseeded for API launch"}'),
  ('66666666-6666-6666-6666-666666666666', 'INDIA_INR_API_V1',    'IN', 'INR', 'api',    NULL, 1.15, false, '2026-03-05 00:00:00+00', NULL, '{"version":"v1","note":"preseeded for API launch"}')
ON CONFLICT (id) DO UPDATE
SET name          = EXCLUDED.name,
    country_code   = EXCLUDED.country_code,
    currency       = EXCLUDED.currency,
    channel        = EXCLUDED.channel,
    tier_code      = EXCLUDED.tier_code,
    multiplier     = EXCLUDED.multiplier,
    is_active      = EXCLUDED.is_active,
    effective_from = EXCLUDED.effective_from,
    effective_to   = EXCLUDED.effective_to,
    metadata_json  = EXCLUDED.metadata_json;

-- ------------------------------------------------------------
-- 5) SKUs (default credits per unit)
-- These are the primitives used by Quote/Reserve/Finalize.
-- ------------------------------------------------------------
INSERT INTO pricing_skus
  (code, name, unit, category, provider_hint, default_unit_credits, status, metadata_json)
VALUES
  -- Face / Image
  ('IMG_STD_RUN',            'Image Generation Standard (≈1MP)',     'run',     'face',     'fal',        5,   'active', '{"notes":"default 1 image output"}'),
  ('IMG_HD_RUN',             'Image Generation HD (≈2MP billed)',    'run',     'face',     'fal',        9,   'active', '{"notes":"HD 1 image output"}'),
  ('FACE_EDIT_PREMIUM_RUN',  'Premium Face Edit / Identity-lock',    'run',     'face',     'openai',     12,  'active', '{"notes":"I2I identity lock / edit"}'),

  -- Audio
  ('AUDIO_TTS_1K_CHARS',     'TTS (per 1k characters)',              '1k_chars','audio',    'azure_tts',  3,   'active', '{"notes":"metered by input chars"}'),

  -- Fusion / Video
  ('FUSION_TALK_MIN',        'Talking Video (per minute)',           'minute',  'fusion',   'heygen',     140, 'active', '{"notes":"provider minute"}'),

  -- Commerce
  ('COMMERCE_VTON_RUN',      'VTON / Try-on (per generation)',       'run',     'commerce', 'fal',        15,  'active', '{"notes":"FASHN/tryon style gen"}'),

  -- Music
  ('MUSIC_TRACK_RUN',        'Music Track Generation (per track)',   'run',     'music',    'fal',        30,  'active', '{"notes":"sonauto/track gen"}'),

  -- Platform infra (used for end-to-end variants)
  ('RENDER_MONTAGE_MIN',     'Montage Render (per minute)',          'minute',  'infra',    'native',     23,  'active', '{"notes":"ffmpeg render time"}'),
  ('OPS_QC_RUN',             'Quality Gate / QC Pass',               'run',     'infra',    'native',     21,  'active', '{"notes":"content + qc gate"}'),

  -- Future: API metering (preseed)
  ('API_1K_CALLS',           'API calls (per 1k calls)',             '1k_calls','api',      'native',     5,   'active', '{"notes":"preseeded for API launch"}')
ON CONFLICT (code) DO UPDATE
SET name                = EXCLUDED.name,
    unit                = EXCLUDED.unit,
    category            = EXCLUDED.category,
    provider_hint       = EXCLUDED.provider_hint,
    default_unit_credits= EXCLUDED.default_unit_credits,
    status              = EXCLUDED.status,
    metadata_json       = EXCLUDED.metadata_json;

-- ------------------------------------------------------------
-- 6) Variants (BOM templates)
-- Variants let you quote a single action like MUSIC_VIDEO_STANDARD and internally expand to SKUs.
-- ------------------------------------------------------------
INSERT INTO pricing_variants (code, name, category, is_active, metadata_json)
VALUES
  ('FACE_IMAGE_STD_BATCH',     'Face Studio: Standard images (batch)',        'face',     true, '{"qty_param":"num_images"}'),
  ('FACE_IMAGE_HD_BATCH',      'Face Studio: HD images (batch)',              'face',     true, '{"qty_param":"num_images"}'),
  ('FACE_EDIT_PREMIUM_BATCH',  'Face Studio: Premium edits (batch)',          'face',     true, '{"qty_param":"num_edits"}'),
  ('AUDIO_TTS',                'Audio Studio: TTS',                            'audio',    true, '{"qty_param":"chars_1k"}'),
  ('FUSION_TALKING_VIDEO',     'Fusion Studio: Talking video',                 'fusion',   true, '{"qty_param":"minutes"}'),
  ('COMMERCE_VTON',            'Commerce Studio: VTON try-on',                  'commerce', true, '{"qty_param":"num_tryons"}'),
  ('MUSIC_TRACK',              'Music Studio: Track generation',               'music',    true, '{"qty_param":"num_tracks"}'),

  -- End-to-end “outcome” variants (fixed BOM)
  ('MUSIC_VIDEO_STANDARD',     'Music Studio: Music Video Standard (≈3min)',   'music',    true, '{"billed_as":"outcome","target":"3min"}'),
  ('MUSIC_VIDEO_PRO',          'Music Studio: Music Video Pro',                'music',    true, '{"billed_as":"outcome","performance_heavy":true}'),
  ('COMMERCE_PRODUCT_PACK',    'Commerce Studio: Product Promo Pack (per product)', 'commerce', true, '{"billed_as":"outcome","includes":"3 imgs + 1 tryon + montage"}')
ON CONFLICT (code) DO UPDATE
SET name         = EXCLUDED.name,
    category     = EXCLUDED.category,
    is_active    = EXCLUDED.is_active,
    metadata_json= EXCLUDED.metadata_json;

-- ------------------------------------------------------------
-- 7) Variant lines (BOM expansion)
-- qty_mode:
--   param   -> quantity comes from request params (quote stage)
--   fixed   -> constant
--   metered -> comes from finalize stage (actual metering)
-- ------------------------------------------------------------

-- FACE_IMAGE_STD_BATCH: IMG_STD_RUN * num_images
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('FACE_IMAGE_STD_BATCH', 'IMG_STD_RUN', 'param', NULL, 'num_images', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- FACE_IMAGE_HD_BATCH: IMG_HD_RUN * num_images
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('FACE_IMAGE_HD_BATCH', 'IMG_HD_RUN', 'param', NULL, 'num_images', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- FACE_EDIT_PREMIUM_BATCH: FACE_EDIT_PREMIUM_RUN * num_edits
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('FACE_EDIT_PREMIUM_BATCH', 'FACE_EDIT_PREMIUM_RUN', 'param', NULL, 'num_edits', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- AUDIO_TTS: AUDIO_TTS_1K_CHARS * chars_1k
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('AUDIO_TTS', 'AUDIO_TTS_1K_CHARS', 'param', NULL, 'chars_1k', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- FUSION_TALKING_VIDEO: FUSION_TALK_MIN * minutes
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('FUSION_TALKING_VIDEO', 'FUSION_TALK_MIN', 'param', NULL, 'minutes', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- COMMERCE_VTON: COMMERCE_VTON_RUN * num_tryons
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('COMMERCE_VTON', 'COMMERCE_VTON_RUN', 'param', NULL, 'num_tryons', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- MUSIC_TRACK: MUSIC_TRACK_RUN * num_tracks
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('MUSIC_TRACK', 'MUSIC_TRACK_RUN', 'param', NULL, 'num_tracks', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- MUSIC_VIDEO_STANDARD (outcome BOM) total credits:
--  1 * FUSION_TALK_MIN (140)
--  1 * MUSIC_TRACK_RUN (30)
-- 12 * IMG_STD_RUN (12*5=60)
--  3 * RENDER_MONTAGE_MIN (3*23=69)
--  => 299 credits
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('MUSIC_VIDEO_STANDARD', 'FUSION_TALK_MIN',    'fixed', 1.0,  '', '{}'::jsonb),
  ('MUSIC_VIDEO_STANDARD', 'MUSIC_TRACK_RUN',    'fixed', 1.0,  '', '{}'::jsonb),
  ('MUSIC_VIDEO_STANDARD', 'IMG_STD_RUN',        'fixed', 12.0, '', '{}'::jsonb),
  ('MUSIC_VIDEO_STANDARD', 'RENDER_MONTAGE_MIN', 'fixed', 3.0,  '', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- MUSIC_VIDEO_PRO target credits:
--  3 * FUSION_TALK_MIN (420)
--  1 * MUSIC_TRACK_RUN (30)
-- 18 * IMG_STD_RUN (90)
--  6 * RENDER_MONTAGE_MIN (138)
--  1 * OPS_QC_RUN (21)
--  => 699 credits
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('MUSIC_VIDEO_PRO', 'FUSION_TALK_MIN',    'fixed', 3.0,  '', '{}'::jsonb),
  ('MUSIC_VIDEO_PRO', 'MUSIC_TRACK_RUN',    'fixed', 1.0,  '', '{}'::jsonb),
  ('MUSIC_VIDEO_PRO', 'IMG_STD_RUN',        'fixed', 18.0, '', '{}'::jsonb),
  ('MUSIC_VIDEO_PRO', 'RENDER_MONTAGE_MIN', 'fixed', 6.0,  '', '{}'::jsonb),
  ('MUSIC_VIDEO_PRO', 'OPS_QC_RUN',         'fixed', 1.0,  '', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- COMMERCE_PRODUCT_PACK (outcome BOM) total credits:
--  3 * IMG_STD_RUN (15)
--  1 * COMMERCE_VTON_RUN (15)
--  3 * RENDER_MONTAGE_MIN (69)
--  => 99 credits
INSERT INTO pricing_variant_lines (variant_code, sku_code, qty_mode, qty_value, qty_param, metadata_json)
VALUES
  ('COMMERCE_PRODUCT_PACK', 'IMG_STD_RUN',        'fixed', 3.0, '', '{}'::jsonb),
  ('COMMERCE_PRODUCT_PACK', 'COMMERCE_VTON_RUN',  'fixed', 1.0, '', '{}'::jsonb),
  ('COMMERCE_PRODUCT_PACK', 'RENDER_MONTAGE_MIN', 'fixed', 3.0, '', '{}'::jsonb)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE
SET qty_value = EXCLUDED.qty_value,
    metadata_json = EXCLUDED.metadata_json;

-- ------------------------------------------------------------
-- 8) PAYG Credit packs (Top-ups)
-- Note: pack price is a "product price" and may differ from credits*money_per_credit.
-- ------------------------------------------------------------
INSERT INTO pricing_credit_packs (code, name, credits, currency, price_money, country_code, is_active, metadata_json)
VALUES
  -- USD global
  ('PACK_USD_1000',  'Starter Pack', 1000,  'USD', 10.00,  '',  true, '{"best_for":"try it"}'),
  ('PACK_USD_5000',  'Value Pack',   5000,  'USD', 40.00,  '',  true, '{"best_for":"most users","tag":"best value"}'),
  ('PACK_USD_15000', 'Pro Pack',     15000, 'USD', 99.00,  '',  true, '{"best_for":"heavy usage"}'),

  -- INR India
  ('PACK_INR_1000',  'Starter Pack', 1000,  'INR',  999.00, 'IN', true, '{"best_for":"try it"}'),
  ('PACK_INR_5000',  'Value Pack',   5000,  'INR', 3999.00, 'IN', true, '{"best_for":"most users","tag":"best value"}'),
  ('PACK_INR_15000', 'Pro Pack',     15000, 'INR', 9999.00, 'IN', true, '{"best_for":"heavy usage"}')
ON CONFLICT (code) DO UPDATE
SET name         = EXCLUDED.name,
    credits      = EXCLUDED.credits,
    currency     = EXCLUDED.currency,
    price_money  = EXCLUDED.price_money,
    country_code = EXCLUDED.country_code,
    is_active    = EXCLUDED.is_active,
    metadata_json= EXCLUDED.metadata_json;

COMMIT;