-- V3 Fusion logical-scene pricing integrity hardening.
--
-- This migration deliberately does NOT change the existing Fusion price,
-- credit rate, variant composition, entitlement, wallet, or ledger behavior.
-- It removes a stale legacy provider hint from the provider-neutral pricing SKU.
-- Provider selection remains an execution/routing concern, not a pricing concern.

BEGIN;

UPDATE public.pricing_skus
SET provider_hint = NULL,
    metadata_json = COALESCE(metadata_json, '{}'::jsonb)
                    || jsonb_build_object(
                         'provider_neutral', true,
                         'pricing_owner', 'svc-pricing',
                         'v3_scene_parent_billing', true,
                         'updated_reason', 'remove_stale_heygen_pricing_metadata'
                       )
WHERE code = 'FUSION_TALK_MIN'
  AND (
    provider_hint IS NOT NULL
    OR COALESCE(metadata_json->>'provider_neutral', 'false') <> 'true'
  );

-- Guard the canonical pricing contract without changing economics.
DO $$
DECLARE
  v_unit text;
  v_credits integer;
  v_provider_hint text;
BEGIN
  SELECT unit, default_unit_credits, provider_hint
  INTO v_unit, v_credits, v_provider_hint
  FROM public.pricing_skus
  WHERE code = 'FUSION_TALK_MIN';

  IF v_unit IS DISTINCT FROM 'minute' THEN
    RAISE EXCEPTION 'FUSION_TALK_MIN must remain minute-based; found %', v_unit;
  END IF;

  IF v_credits IS NULL OR v_credits <= 0 THEN
    RAISE EXCEPTION 'FUSION_TALK_MIN must retain a positive DB-owned credit rate';
  END IF;

  IF v_provider_hint IS NOT NULL AND btrim(v_provider_hint) <> '' THEN
    RAISE EXCEPTION 'FUSION_TALK_MIN pricing SKU must be provider-neutral; found %', v_provider_hint;
  END IF;
END $$;

COMMIT;
