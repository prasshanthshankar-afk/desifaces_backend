-- V3-C6 follow-up: normalize pricing credit-lot metadata JSONB.
--
-- svc-pricing uses an asyncpg JSON/JSONB codec. Historical call sites sometimes
-- passed json.dumps(...) output into JSONB parameters, causing the codec to
-- serialize that string again. The resulting JSON string scalar makes
-- expressions such as metadata_json->>'cycle_key' return NULL and can make a
-- spent subscription lot look like an unversioned legacy lot at renewal.
--
-- This migration is idempotent. It:
--   1) safely converts double-encoded object/list metadata back to JSONB,
--   2) repairs existing pricing_credit_lots rows, and
--   3) normalizes future INSERT/metadata UPDATE writes at the DB boundary.

BEGIN;

CREATE OR REPLACE FUNCTION public.df_v3_normalize_jsonb_container(p_value jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  v_text text;
  v_decoded jsonb;
BEGIN
  IF p_value IS NULL OR jsonb_typeof(p_value) <> 'string' THEN
    RETURN COALESCE(p_value, '{}'::jsonb);
  END IF;

  v_text := p_value #>> '{}';
  IF v_text IS NULL OR btrim(v_text) = '' THEN
    RETURN p_value;
  END IF;

  BEGIN
    v_decoded := v_text::jsonb;
  EXCEPTION WHEN others THEN
    RETURN p_value;
  END;

  IF jsonb_typeof(v_decoded) IN ('object', 'array') THEN
    RETURN v_decoded;
  END IF;

  RETURN p_value;
END;
$$;

UPDATE public.pricing_credit_lots
SET metadata_json = public.df_v3_normalize_jsonb_container(metadata_json),
    updated_at = now()
WHERE metadata_json IS NOT NULL
  AND jsonb_typeof(metadata_json) = 'string'
  AND public.df_v3_normalize_jsonb_container(metadata_json) IS DISTINCT FROM metadata_json;

CREATE OR REPLACE FUNCTION public.df_v3_credit_lot_metadata_json_normalize()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.metadata_json := public.df_v3_normalize_jsonb_container(
    COALESCE(NEW.metadata_json, '{}'::jsonb)
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_df_v3_credit_lot_metadata_json_normalize
  ON public.pricing_credit_lots;
CREATE TRIGGER trg_df_v3_credit_lot_metadata_json_normalize
BEFORE INSERT OR UPDATE OF metadata_json ON public.pricing_credit_lots
FOR EACH ROW
EXECUTE FUNCTION public.df_v3_credit_lot_metadata_json_normalize();

COMMIT;
