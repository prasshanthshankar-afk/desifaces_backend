-- services/svc-pricing/app/app/migrations/0003_pricing_admin_seed.sql
-- Admin seed: default pricebook selection rules + a few sample overrides.
-- Uses metadata_json on pricing_pricebooks to drive deterministic selection in svc-pricing.

BEGIN;

-- ------------------------------------------------------------
-- 1) Default selection rules stored on the "GLOBAL" pricebooks.
-- We store the same rule object on all pricebooks for easy retrieval.
--
-- Selector intention (svc-pricing services/pricebook_selector.py):
--  - Filter active pricebooks matching (channel, currency)
--  - Prefer exact country match, else global (country_code NULL/''), else any
--  - Prefer exact tier_code match, else tier_code IS NULL, else any
--  - Prefer latest effective_from
--
-- Note: Your pricebooks in 0002 use fixed UUIDs. We update metadata_json on those.
-- ------------------------------------------------------------

-- Helper: JSON policy blob for selector + reservations
-- - countries: match country_code='IN' etc
-- - tiers: match entitlements
-- - fallback: global pricebook
-- - reservation_ttl_seconds: used by reserve() default expiry
-- - allow_overage_pct: consumer guardrail (optional)
-- - api_channel_multiplier_default: used when API is enabled later
WITH policy AS (
  SELECT
    '{
      "selector_rules": {
        "version": "v1",
        "match_priority": [
          "channel",
          "currency",
          "country_exact_then_global",
          "tier_exact_then_any",
          "effective_from_latest"
        ],
        "country_global_values": ["", null],
        "tier_global_values": ["", null],
        "fallback_behavior": "use_global_if_country_missing",
        "errors": {
          "no_pricebook": "PRICING_NO_ACTIVE_PRICEBOOK"
        }
      },
      "reservation_policy": {
        "default_ttl_seconds": 900,
        "max_ttl_seconds": 3600,
        "allow_overage_pct_consumer": 0.10,
        "allow_overage_pct_business": 0.25
      },
      "display_policy": {
        "show_alt_currency": true,
        "alt_currency": "USD",
        "rounding": "ceil"
      },
      "api_launch_policy": {
        "enabled": false,
        "default_multiplier": 1.15,
        "note": "API pricebooks preseeded in 0002; enable in ~3 months."
      }
    }'::jsonb AS j
)

UPDATE pricing_pricebooks pb
SET metadata_json =
  -- merge existing metadata (e.g. {"version":"v1"}) with the admin policy
  COALESCE(pb.metadata_json, '{}'::jsonb) || (SELECT j FROM policy)
WHERE pb.id IN (
  '11111111-1111-1111-1111-111111111111', -- GLOBAL_USD_WEB_V1
  '22222222-2222-2222-2222-222222222222', -- GLOBAL_USD_MOBILE_V1
  '33333333-3333-3333-3333-333333333333', -- INDIA_INR_WEB_V1
  '44444444-4444-4444-4444-444444444444', -- INDIA_INR_MOBILE_V1
  '55555555-5555-5555-5555-555555555555', -- GLOBAL_USD_API_V1 (inactive)
  '66666666-6666-6666-6666-666666666666'  -- INDIA_INR_API_V1  (inactive)
);

-- ------------------------------------------------------------
-- 2) Example per-SKU overrides (optional, small, safe).
-- These show how you can tune pricing per region/channel without changing core SKUs.
--
-- A) India web/mobile: slightly cheaper VTON to drive SMB adoption
-- B) India web/mobile: slightly cheaper Talking Video minute to stay competitive
-- C) API pricebooks (inactive now): slightly higher per-minute for videos (multiplier already covers this, but shown as example)
-- ------------------------------------------------------------

-- INDIA_INR_WEB_V1 overrides
INSERT INTO pricing_sku_prices (pricebook_id, sku_code, unit_credits_override, unit_money_override, min_qty, max_qty, metadata_json)
VALUES
  ('33333333-3333-3333-3333-333333333333', 'COMMERCE_VTON_RUN', 14, NULL, NULL, NULL, '{"reason":"India intro offer for SMB commerce adoption"}'),
  ('33333333-3333-3333-3333-333333333333', 'FUSION_TALK_MIN',  135, NULL, NULL, NULL, '{"reason":"India competitive minute pricing"}')
ON CONFLICT (pricebook_id, sku_code) DO UPDATE
SET unit_credits_override = EXCLUDED.unit_credits_override,
    unit_money_override   = EXCLUDED.unit_money_override,
    min_qty               = EXCLUDED.min_qty,
    max_qty               = EXCLUDED.max_qty,
    metadata_json         = EXCLUDED.metadata_json;

-- INDIA_INR_MOBILE_V1 overrides
INSERT INTO pricing_sku_prices (pricebook_id, sku_code, unit_credits_override, unit_money_override, min_qty, max_qty, metadata_json)
VALUES
  ('44444444-4444-4444-4444-444444444444', 'COMMERCE_VTON_RUN', 14, NULL, NULL, NULL, '{"reason":"India intro offer for SMB commerce adoption"}'),
  ('44444444-4444-4444-4444-444444444444', 'FUSION_TALK_MIN',  135, NULL, NULL, NULL, '{"reason":"India competitive minute pricing"}')
ON CONFLICT (pricebook_id, sku_code) DO UPDATE
SET unit_credits_override = EXCLUDED.unit_credits_override,
    unit_money_override   = EXCLUDED.unit_money_override,
    min_qty               = EXCLUDED.min_qty,
    max_qty               = EXCLUDED.max_qty,
    metadata_json         = EXCLUDED.metadata_json;

-- API examples (still inactive; for later)
INSERT INTO pricing_sku_prices (pricebook_id, sku_code, unit_credits_override, unit_money_override, min_qty, max_qty, metadata_json)
VALUES
  ('55555555-5555-5555-5555-555555555555', 'FUSION_TALK_MIN', 150, NULL, NULL, NULL, '{"reason":"API channel guardrail (example)"}'),
  ('66666666-6666-6666-6666-666666666666', 'FUSION_TALK_MIN', 150, NULL, NULL, NULL, '{"reason":"API channel guardrail (example)"}')
ON CONFLICT (pricebook_id, sku_code) DO UPDATE
SET unit_credits_override = EXCLUDED.unit_credits_override,
    unit_money_override   = EXCLUDED.unit_money_override,
    min_qty               = EXCLUDED.min_qty,
    max_qty               = EXCLUDED.max_qty,
    metadata_json         = EXCLUDED.metadata_json;

-- ------------------------------------------------------------
-- 3) Optional: Mark which pricebooks are "defaults" for fast selection.
-- This is purely metadata; the selector can use it as a tie-breaker.
-- ------------------------------------------------------------
UPDATE pricing_pricebooks
SET metadata_json = COALESCE(metadata_json,'{}'::jsonb) || jsonb_build_object(
  'is_default', true,
  'default_for', CASE
    WHEN id IN ('11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222') THEN 'global_usd'
    WHEN id IN ('33333333-3333-3333-3333-333333333333','44444444-4444-4444-4444-444444444444') THEN 'india_inr'
    ELSE 'api_preseed'
  END
)
WHERE id IN (
  '11111111-1111-1111-1111-111111111111',
  '22222222-2222-2222-2222-222222222222',
  '33333333-3333-3333-3333-333333333333',
  '44444444-4444-4444-4444-444444444444'
);

COMMIT;