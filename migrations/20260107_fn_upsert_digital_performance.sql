-- Migration: Fix fn_upsert_digital_performance to work with partial unique index
-- Date: 2026-01-07
-- Purpose: Add WHERE clause to ON CONFLICT to match the partial unique constraint

CREATE OR REPLACE FUNCTION public.fn_upsert_digital_performance(
  p_user_id integer, 
  p_provider text, 
  p_provider_job_id text, 
  p_status text, 
  p_share_url text, 
  p_meta_json jsonb
)
RETURNS uuid
LANGUAGE plpgsql
AS $function$
DECLARE
  v_id uuid;
BEGIN
  -- Normalize provider/status
  IF p_provider IS NULL OR length(trim(p_provider)) = 0 THEN
    RAISE EXCEPTION 'provider must be non-empty';
  END IF;

  IF p_status NOT IN ('processing','ready','failed') THEN
    RAISE EXCEPTION 'invalid status: %', p_status;
  END IF;

  -- If provider_job_id is present, we can upsert using the existing unique constraint
  IF p_provider_job_id IS NOT NULL AND length(trim(p_provider_job_id)) > 0 THEN
    INSERT INTO public.digital_performances (
      user_id, provider, provider_job_id, status, share_url, meta_json, created_at, updated_at
    ) VALUES (
      p_user_id, p_provider, p_provider_job_id, p_status, p_share_url,
      COALESCE(p_meta_json, '{}'::jsonb),
      now(), now()
    )
    ON CONFLICT (provider, provider_job_id) WHERE provider_job_id IS NOT NULL
    DO UPDATE SET
      user_id = EXCLUDED.user_id,
      status = EXCLUDED.status,
      share_url = COALESCE(EXCLUDED.share_url, public.digital_performances.share_url),
      meta_json = public.digital_performances.meta_json || EXCLUDED.meta_json,
      updated_at = now()
    RETURNING id INTO v_id;

    RETURN v_id;
  END IF;

  -- If no provider_job_id, create a fresh performance
  INSERT INTO public.digital_performances (
    user_id, provider, status, share_url, meta_json, created_at, updated_at
  ) VALUES (
    p_user_id, p_provider, p_status, p_share_url, COALESCE(p_meta_json, '{}'::jsonb), now(), now()
  )
  RETURNING id INTO v_id;

  RETURN v_id;
END;
$function$;