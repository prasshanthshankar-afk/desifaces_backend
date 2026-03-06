-- migrations/2026_02_23_saree_model_training_qc_add_checkpoint.sql
BEGIN;

-- Sanity: required table
DO $$
BEGIN
  IF to_regclass('public.model_checkpoints') IS NULL THEN
    RAISE EXCEPTION 'Missing table: model_checkpoints';
  END IF;
END $$;

-- Create/normalize a deprecated QC placeholder checkpoint.
-- IMPORTANT:
--   - Must NOT be 'succeeded'
--   - Must NOT contain REPLACE_ME weights/root paths
DO $$
DECLARE
  v_ckpt_id uuid := '1570cbfd-2004-40ea-9ac9-ec2156e4d97c'::uuid;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM model_checkpoints WHERE id = v_ckpt_id) THEN
    INSERT INTO model_checkpoints (
      id,
      model_family,
      base_model,
      status,
      code_git_sha,
      config_json,
      hyperparams_json,
      metrics_json,
      artifacts_json,
      notes,
      created_by,
      created_at,
      updated_at
    )
    VALUES (
      v_ckpt_id,
      'saree_drape',
      'flux.1-dev',
      'deprecated',
      NULL,
      jsonb_build_object(
        'drape_template_slug', 'saree/nivi/v1',
        'task', 'vton_saree'
      ),
      jsonb_build_object(
        'train_steps', 8000,
        'lr', 1e-4,
        'lora_rank', 16
      ),
      jsonb_build_object(
        'val_loss', 0.0,
        'notes', 'bootstrap placeholder'
      ),
      '{}'::jsonb,
      'bootstrap placeholder (deprecated): QC-only; not routable',
      NULL,
      now(),
      now()
    );
  ELSE
    -- Normalize existing row ONLY if it violates expectations.
    IF EXISTS (
      SELECT 1
      FROM model_checkpoints
      WHERE id = v_ckpt_id
        AND (
          status = 'succeeded'
          OR (artifacts_json->'weights'->>'path') ILIKE '%REPLACE_ME%'
          OR coalesce(artifacts_json->>'checkpoint_root','') ILIKE '%REPLACE_ME%'
        )
    ) THEN
      UPDATE model_checkpoints
      SET
        status = CASE WHEN status = 'succeeded' THEN 'failed' ELSE status END,
        artifacts_json = '{}'::jsonb,
        notes = coalesce(notes,'') || ' | normalized by migration: QC placeholder must not be succeeded or have placeholder artifacts',
        updated_at = now()
      WHERE id = v_ckpt_id;
    END IF;
  END IF;
END $$;

COMMIT;