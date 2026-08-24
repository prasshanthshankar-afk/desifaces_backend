-- V3 multi-person premium pricing scaffold.
--
-- PURPOSE
--   Reserve a DB-backed pricing package for future Multi-Person premium charging
--   while keeping today's Face/Audio/Fusion pricing behavior completely unchanged.
--
-- ACTIVATION SAFETY
--   * The premium SKU starts at 0 credits and INACTIVE.
--   * The premium variant starts INACTIVE.
--   * The package feature flag starts disabled with billing_mode=disabled.
--   * Existing face.creator.generate.t2i / face.creator.generate.i2i pricing is not modified.
--   * Existing wallet, reservation, entitlement, ledger, pricebook, and subscription
--     ownership remains entirely in svc-pricing.
--
-- FUTURE PRODUCT ACTIVATION (separate approved change)
--   1. Set a reviewed price for face.multi_person.premium.surcharge through the
--      existing SKU/pricebook catalog.
--   2. Activate FACE_MULTI_PERSON_PREMIUM and the premium SKU.
--   3. Switch package.face.multi_person.premium from disabled -> shadow/free for
--      certification, then -> bill when approved.
--   4. Route the Director's multi-person premium component to this variant while
--      preserving the existing per-participant Face pricing quote/hold/commit path.
--
-- This migration is intentionally catalog-only. Applying it has zero billing impact.

BEGIN;

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
VALUES (
  'face.multi_person.premium.surcharge',
  'Multi-Person Face premium surcharge',
  'participant',
  'face',
  NULL,
  0,
  'inactive',
  jsonb_build_object(
    'package', 'multi_person',
    'pricing_role', 'premium_surcharge',
    'current_billing_impact', 'none',
    'activation', 'product_approval_required',
    'base_pricing', 'existing_face_creator_pricing'
  )
)
ON CONFLICT (code) DO NOTHING;

INSERT INTO pricing_variants (
  code,
  name,
  category,
  is_active,
  metadata_json
)
VALUES (
  'FACE_MULTI_PERSON_PREMIUM',
  'Multi-Person Face Premium',
  'face',
  false,
  jsonb_build_object(
    'package', 'multi_person',
    'current_billing_impact', 'none',
    'activation', 'product_approval_required',
    'base_pricing', 'existing_face_creator_pricing',
    'premium_sku', 'face.multi_person.premium.surcharge'
  )
)
ON CONFLICT (code) DO NOTHING;

INSERT INTO pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_value,
  qty_param,
  metadata_json
)
VALUES (
  'FACE_MULTI_PERSON_PREMIUM',
  'face.multi_person.premium.surcharge',
  'param',
  NULL,
  'premium_participants',
  jsonb_build_object(
    'description', 'Future premium participant quantity; dormant until package activation',
    'current_billing_impact', 'none'
  )
)
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO NOTHING;

INSERT INTO pricing_feature_flags (
  code,
  scope,
  country_code,
  tier_code,
  channel,
  enabled,
  billing_mode,
  priority,
  metadata_json
)
VALUES (
  'package.face.multi_person.premium',
  'global',
  '',
  '',
  '',
  false,
  'disabled',
  100,
  jsonb_build_object(
    'variant_code', 'FACE_MULTI_PERSON_PREMIUM',
    'premium_sku', 'face.multi_person.premium.surcharge',
    'current_billing_impact', 'none',
    'activation', 'product_approval_required'
  )
)
ON CONFLICT (code) DO NOTHING;

COMMIT;
