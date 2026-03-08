-- services/svc-pricing/app/app/migrations/0008_pricing_cost_seed.sql
-- Seed COGS assumptions (INTERNAL economics only; does NOT change customer billing).
-- Idempotent via ON CONFLICT (sku_code, component_code, effective_from).
-- IMPORTANT: Run 0007_pricing_sku_costs.sql first.

BEGIN;

-- Fixed effective_from to keep idempotent inserts stable
-- (You can bump this timestamp later when you change assumptions.)
-- Format: YYYY-MM-DD HH:MM:SS+00
-- ------------------------------------------------------------

-- 1) HeyGen subscription amortization: $159/month
-- Assumption: 120 minutes/month => $1.325/min
INSERT INTO pricing_sku_costs (
  sku_code, component_code, cost_model, cost_currency,
  variable_cost_money, fixed_monthly_cost_money, assumed_monthly_units,
  is_active, effective_from, effective_to, metadata_json
) VALUES (
  'FUSION_TALK_MIN',
  'heygen_subscription',
  'amortized',
  'USD',
  0.0,
  159.0,
  120.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"subscription","assumption":"$159/mo amortized over 120 min/mo","notes":"Adjust assumed_monthly_units to match actual usage/quota."}'::jsonb
)
ON CONFLICT (sku_code, component_code, effective_from) DO UPDATE
SET cost_model             = EXCLUDED.cost_model,
    cost_currency          = EXCLUDED.cost_currency,
    variable_cost_money    = EXCLUDED.variable_cost_money,
    fixed_monthly_cost_money = EXCLUDED.fixed_monthly_cost_money,
    assumed_monthly_units  = EXCLUDED.assumed_monthly_units,
    is_active              = EXCLUDED.is_active,
    effective_to           = EXCLUDED.effective_to,
    metadata_json          = EXCLUDED.metadata_json;

-- 2) fal.ai subscription amortization: $99/month
-- Allocation across SKUs (so we don’t double-count):
--   IMG_STD_RUN   60% => $59.40 / 3000 runs  => $0.0198/run
--   IMG_HD_RUN    20% => $19.80 / 1000 runs  => $0.0198/run
--   COMMERCE_VTON 10% => $9.90  / 500 runs   => $0.0198/run
--   MUSIC_TRACK   10% => $9.90  / 200 runs   => $0.0495/run
INSERT INTO pricing_sku_costs (
  sku_code, component_code, cost_model, cost_currency,
  variable_cost_money, fixed_monthly_cost_money, assumed_monthly_units,
  is_active, effective_from, effective_to, metadata_json
) VALUES
(
  'IMG_STD_RUN',
  'fal_subscription_alloc',
  'amortized',
  'USD',
  0.0,
  59.40,
  3000.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"subscription","allocation_pct":0.60,"assumption":"$99/mo allocated","notes":"Adjust runs/month + allocation as usage stabilizes."}'::jsonb
),
(
  'IMG_HD_RUN',
  'fal_subscription_alloc',
  'amortized',
  'USD',
  0.0,
  19.80,
  1000.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"subscription","allocation_pct":0.20,"assumption":"$99/mo allocated"}'::jsonb
),
(
  'COMMERCE_VTON_RUN',
  'fal_subscription_alloc',
  'amortized',
  'USD',
  0.0,
  9.90,
  500.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"subscription","allocation_pct":0.10,"assumption":"$99/mo allocated"}'::jsonb
),
(
  'MUSIC_TRACK_RUN',
  'fal_subscription_alloc',
  'amortized',
  'USD',
  0.0,
  9.90,
  200.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"subscription","allocation_pct":0.10,"assumption":"$99/mo allocated","notes":"Lower units/month => higher COGS per run."}'::jsonb
)
ON CONFLICT (sku_code, component_code, effective_from) DO UPDATE
SET cost_model             = EXCLUDED.cost_model,
    cost_currency          = EXCLUDED.cost_currency,
    variable_cost_money    = EXCLUDED.variable_cost_money,
    fixed_monthly_cost_money = EXCLUDED.fixed_monthly_cost_money,
    assumed_monthly_units  = EXCLUDED.assumed_monthly_units,
    is_active              = EXCLUDED.is_active,
    effective_to           = EXCLUDED.effective_to,
    metadata_json          = EXCLUDED.metadata_json;

-- 3) Azure infra amortization (PLACEHOLDER total = $300/mo)
-- Allocation totals $300 (placeholder—replace with real Azure spend later).
INSERT INTO pricing_sku_costs (
  sku_code, component_code, cost_model, cost_currency,
  variable_cost_money, fixed_monthly_cost_money, assumed_monthly_units,
  is_active, effective_from, effective_to, metadata_json
) VALUES
(
  'FUSION_TALK_MIN',
  'azure_infra_alloc',
  'amortized',
  'USD',
  0.0,
  120.0,
  600.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"azure","placeholder":true,"assumption":"$300/mo total infra placeholder","update_required":true}'::jsonb
),
(
  'IMG_STD_RUN',
  'azure_infra_alloc',
  'amortized',
  'USD',
  0.0,
  60.0,
  6000.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"azure","placeholder":true,"update_required":true}'::jsonb
),
(
  'IMG_HD_RUN',
  'azure_infra_alloc',
  'amortized',
  'USD',
  0.0,
  30.0,
  2000.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"azure","placeholder":true,"update_required":true}'::jsonb
),
(
  'COMMERCE_VTON_RUN',
  'azure_infra_alloc',
  'amortized',
  'USD',
  0.0,
  60.0,
  2000.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"azure","placeholder":true,"update_required":true}'::jsonb
),
(
  'MUSIC_TRACK_RUN',
  'azure_infra_alloc',
  'amortized',
  'USD',
  0.0,
  15.0,
  500.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"azure","placeholder":true,"update_required":true}'::jsonb
),
(
  'RENDER_MONTAGE_MIN',
  'azure_infra_alloc',
  'amortized',
  'USD',
  0.0,
  10.0,
  2000.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"azure","placeholder":true,"update_required":true}'::jsonb
),
(
  'OPS_QC_RUN',
  'azure_infra_alloc',
  'amortized',
  'USD',
  0.0,
  5.0,
  5000.0,
  true,
  '2026-03-05 00:00:00+00',
  NULL,
  '{"source":"azure","placeholder":true,"update_required":true}'::jsonb
)
ON CONFLICT (sku_code, component_code, effective_from) DO UPDATE
SET cost_model             = EXCLUDED.cost_model,
    cost_currency          = EXCLUDED.cost_currency,
    variable_cost_money    = EXCLUDED.variable_cost_money,
    fixed_monthly_cost_money = EXCLUDED.fixed_monthly_cost_money,
    assumed_monthly_units  = EXCLUDED.assumed_monthly_units,
    is_active              = EXCLUDED.is_active,
    effective_to           = EXCLUDED.effective_to,
    metadata_json          = EXCLUDED.metadata_json;

COMMIT;