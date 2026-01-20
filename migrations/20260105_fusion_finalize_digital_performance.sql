-- ============================================================================
-- DesiFaces.ai - Fusion Finalization (digital_performances + fusion_job_outputs)
-- Date: 2026-01-05
--
-- Purpose:
--  1) Provide a rock-solid, repeatable way to write Fusion "final outputs"
--     using existing tables:
--       - digital_performances
--       - fusion_job_outputs
--       - artifacts
--       - media_assets / media_asset_versions (optional in Phase-1)
--  2) Ensure essential indexes exist
--  3) Provide helper functions for:
--       - create/get digital_performance for a job/provider_job_id
--       - mark ready/failed
--       - upsert fusion_job_outputs(job_id -> performance_id)
--
-- Safe to re-run.
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1) Index Hardening (minimal + useful)
-- ----------------------------------------------------------------------------

-- studio_jobs: idempotent submit already exists (you currently have duplicates).
-- Keep the constraint; no changes required here.

-- artifacts: common query pattern is job_id + kind
CREATE INDEX IF NOT EXISTS idx_artifacts_job_kind_created
ON public.artifacts (job_id, kind, created_at DESC);

-- digital_performances: already has uq_digital_performances_provider_job (great).
-- Add (user_id, provider_job_id) helper index for faster lookups (optional).
CREATE INDEX IF NOT EXISTS idx_digital_performances_user_provider_job
ON public.digital_performances (user_id, provider, provider_job_id)
WHERE provider_job_id IS NOT NULL;

-- fusion_job_outputs: uq_fusion_job_outputs_job already exists.

-- ----------------------------------------------------------------------------
-- 2) Helper Functions
-- ----------------------------------------------------------------------------

-- 2.1 Create or get a digital_performance row (idempotent by provider+provider_job_id)
-- Returns digital_performances.id
CREATE OR REPLACE FUNCTION public.fn_upsert_digital_performance(
  p_user_id int,
  p_provider text,
  p_provider_job_id text,
  p_status text,
  p_share_url text,
  p_meta_json jsonb
) RETURNS uuid
LANGUAGE plpgsql
AS $$
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
    ON CONFLICT (provider, provider_job_id)
    DO UPDATE SET
      user_id = EXCLUDED.user_id,
      status = EXCLUDED.status,
      share_url = COALESCE(EXCLUDED.share_url, public.digital_performances.share_url),
      meta_json = public.digital_performances.meta_json || EXCLUDED.meta_json,
      updated_at = now()
    RETURNING id INTO v_id;

    RETURN v_id;
  END IF;

  -- If no provider_job_id, create a fresh performance (less ideal, but supported)
  INSERT INTO public.digital_performances (
    user_id, provider, status, share_url, meta_json, created_at, updated_at
  ) VALUES (
    p_user_id, p_provider, p_status, p_share_url, COALESCE(p_meta_json, '{}'::jsonb), now(), now()
  )
  RETURNING id INTO v_id;

  RETURN v_id;
END;
$$;

-- 2.2 Upsert fusion_job_outputs(job_id -> digital_performance_id)
CREATE OR REPLACE FUNCTION public.fn_upsert_fusion_job_output(
  p_job_id uuid,
  p_digital_performance_id uuid
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  -- fusion_job_outputs has UNIQUE(job_id) and a trigger to enforce job type.
  INSERT INTO public.fusion_job_outputs (job_id, digital_performance_id, created_at)
  VALUES (p_job_id, p_digital_performance_id, now())
  ON CONFLICT (job_id)
  DO UPDATE SET
    digital_performance_id = EXCLUDED.digital_performance_id;
END;
$$;

-- 2.3 Mark a performance READY + attach share_url/provider_job_id metadata
CREATE OR REPLACE FUNCTION public.fn_mark_digital_performance_ready(
  p_digital_performance_id uuid,
  p_share_url text,
  p_meta_json jsonb
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE public.digital_performances
  SET status='ready',
      share_url = COALESCE(p_share_url, share_url),
      meta_json = meta_json || COALESCE(p_meta_json, '{}'::jsonb),
      updated_at = now()
  WHERE id = p_digital_performance_id;
END;
$$;

-- 2.4 Mark a performance FAILED with error details
CREATE OR REPLACE FUNCTION public.fn_mark_digital_performance_failed(
  p_digital_performance_id uuid,
  p_error_code text,
  p_error_message text,
  p_meta_json jsonb
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE public.digital_performances
  SET status='failed',
      meta_json = meta_json || jsonb_build_object(
        'error_code', p_error_code,
        'error_message', p_error_message
      ) || COALESCE(p_meta_json, '{}'::jsonb),
      updated_at = now()
  WHERE id = p_digital_performance_id;
END;
$$;

COMMIT;

-- ============================================================================
-- END
-- ============================================================================