-- migrations/2026_02_28_saree_checkpoint_guardrails.sql
BEGIN;

DO $$
BEGIN
  IF to_regclass('public.model_checkpoints') IS NULL THEN
    RAISE EXCEPTION 'Missing table: model_checkpoints';
  END IF;
END $$;

-- Invalidate bogus succeeded checkpoints
UPDATE model_checkpoints
SET
  status = 'failed',
  notes = coalesce(notes,'') || ' | INVALIDATED: succeeded checkpoint had missing/placeholder artifacts',
  updated_at = now()
WHERE status = 'succeeded'
  AND (
    coalesce(artifacts_json->'weights'->>'path','') = ''
    OR (artifacts_json->'weights'->>'path') ILIKE '%REPLACE_ME%'
    OR coalesce(artifacts_json->>'checkpoint_root','') ILIKE '%REPLACE_ME%'
  );

-- Add constraint: succeeded requires real weights
DO $$
BEGIN
  ALTER TABLE model_checkpoints
    ADD CONSTRAINT model_checkpoints_succeeded_requires_weights_chk
    CHECK (
      status <> 'succeeded'
      OR (
        coalesce(artifacts_json->'weights'->>'path','') <> ''
        AND (artifacts_json->'weights'->>'path') NOT ILIKE '%REPLACE_ME%'
        AND coalesce(artifacts_json->>'checkpoint_root','') NOT ILIKE '%REPLACE_ME%'
      )
    );
EXCEPTION
  WHEN duplicate_object THEN
    NULL;
END $$;

COMMIT;