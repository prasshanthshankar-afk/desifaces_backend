-- 011_core_audit_and_feature_flags.sql
-- Core audit logging + feature flags
-- MUST exist before Face/Fusion go live
-- Safe, append-only, production-grade

BEGIN;

-- =========================================================
-- AUDIT LOG
-- =========================================================
-- Append-only. Never update/delete rows.
-- Used for:
-- - auth events
-- - admin actions
-- - billing debits/credits
-- - provider calls (fal / heygen)
-- - job lifecycle transitions

CREATE TABLE IF NOT EXISTS core.audit_log (
  id bigserial PRIMARY KEY,

  -- who did it (null = system/worker)
  actor_user_id uuid NULL REFERENCES core.users(id),

  -- what happened
  action text NOT NULL,
  entity_type text NOT NULL,   -- e.g. user, session, face_job, fusion_job, billing, provider_call
  entity_id text NOT NULL,     -- uuid or external id

  -- request correlation
  request_id text NULL,

  -- before/after snapshots (optional but powerful)
  before_json jsonb NULL,
  after_json jsonb NULL,

  -- metadata
  ip text NULL,
  user_agent text NULL,

  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_log_created_at
  ON core.audit_log (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_audit_log_actor
  ON core.audit_log (actor_user_id);

CREATE INDEX IF NOT EXISTS ix_audit_log_entity
  ON core.audit_log (entity_type, entity_id);

-- Guardrail: prevent accidental updates/deletes
CREATE OR REPLACE FUNCTION core.audit_log_no_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_log_no_update ON core.audit_log;
DROP TRIGGER IF EXISTS trg_audit_log_no_delete ON core.audit_log;

CREATE TRIGGER trg_audit_log_no_update
BEFORE UPDATE ON core.audit_log
FOR EACH ROW EXECUTE FUNCTION core.audit_log_no_mutation();

CREATE TRIGGER trg_audit_log_no_delete
BEFORE DELETE ON core.audit_log
FOR EACH ROW EXECUTE FUNCTION core.audit_log_no_mutation();

-- =========================================================
-- FEATURE FLAGS
-- =========================================================
-- Used for:
-- - enable/disable studios
-- - provider routing (fal / heygen)
-- - tier gating
-- - emergency kill-switches

CREATE TABLE IF NOT EXISTS core.feature_flags (
  id bigserial PRIMARY KEY,

  -- scope of the flag
  scope text NOT NULL CHECK (scope IN ('global','tier','user')),
  scope_key text NULL,   -- tier name OR user_id (text)

  flag_key text NOT NULL, -- e.g. fusion.heygen.enabled
  enabled boolean NOT NULL DEFAULT false,

  -- optional structured config
  config_json jsonb NOT NULL DEFAULT '{}'::jsonb,

  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_feature_flag UNIQUE (scope, scope_key, flag_key)
);

CREATE INDEX IF NOT EXISTS ix_feature_flags_lookup
  ON core.feature_flags (flag_key, scope, scope_key);

-- Seed essential flags (safe defaults)
INSERT INTO core.feature_flags (scope, scope_key, flag_key, enabled)
VALUES
  ('global', NULL, 'face.studio.enabled', true),
  ('global', NULL, 'fusion.studio.enabled', false),
  ('global', NULL, 'fusion.heygen.enabled', false),
  ('global', NULL, 'billing.enforce_caps.enabled', true)
ON CONFLICT DO NOTHING;

COMMIT;

