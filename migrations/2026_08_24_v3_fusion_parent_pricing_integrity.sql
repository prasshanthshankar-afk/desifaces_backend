-- V3 Fusion logical-scene pricing integrity hardening.
--
-- This migration deliberately does NOT change the existing Fusion price,
-- credit rate, variant composition, entitlement, wallet, or ledger behavior.
-- It removes a stale legacy provider hint from the provider-neutral pricing SKU
-- and enforces that a Fusion scene cannot cross HITL approval before its single
-- parent pricing lifecycle is committed.

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

-- A Fusion candidate is not approvable until the logical-scene parent charge is
-- durably committed. Enforce this in PostgreSQL so UI/API/worker callers cannot
-- bypass the pricing lifecycle by writing the review decision directly.
CREATE OR REPLACE FUNCTION public.v3_guard_fusion_review_pricing_commit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_stage_type text;
  v_pricing_state text;
BEGIN
  IF NEW.decision IS DISTINCT FROM 'approved' THEN
    RETURN NEW;
  END IF;

  SELECT s.stage_type,
         COALESCE(s.metadata_json #>> '{fusion_parent_pricing,state}', '')
  INTO v_stage_type, v_pricing_state
  FROM public.v3_studio_stage_runs s
  WHERE s.stage_run_id = NEW.stage_run_id;

  IF v_stage_type = 'fusion' AND v_pricing_state <> 'committed' THEN
    RAISE EXCEPTION USING
      ERRCODE = '23514',
      MESSAGE = 'fusion_parent_pricing_not_committed';
  END IF;

  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_v3_guard_fusion_review_pricing_commit
ON public.v3_studio_review_items;

CREATE TRIGGER trg_v3_guard_fusion_review_pricing_commit
BEFORE INSERT OR UPDATE OF decision
ON public.v3_studio_review_items
FOR EACH ROW
EXECUTE FUNCTION public.v3_guard_fusion_review_pricing_commit();

COMMIT;
