-- migrations/030_face_jobs.sql
-- Face Studio durable entities (profiles) + link job outputs to profiles
-- Depends on: 020_studio_kernel.sql, 020_media_assets.sql

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -------------------------------------------------------------------
-- face_profiles: reusable Face Cards / identities
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS face_profiles (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                integer NOT NULL,
  display_name           text,

  -- Primary image for the face card (points to media_assets)
  primary_image_asset_id uuid REFERENCES media_assets(id) ON DELETE SET NULL,

  -- active|archived
  status                 text NOT NULL DEFAULT 'active',

  -- Attributes/tags: e.g., {"gender":"female","age_range":"30-40","style_tags":["cinematic"]}
  attributes_json        jsonb NOT NULL DEFAULT '{}'::jsonb,
  meta_json              jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_face_profiles_status CHECK (status IN ('active','archived'))
);

CREATE INDEX IF NOT EXISTS idx_face_profiles_user_created
  ON face_profiles (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_face_profiles_user_status
  ON face_profiles (user_id, status);

CREATE INDEX IF NOT EXISTS idx_face_profiles_primary_asset
  ON face_profiles (primary_image_asset_id);

-- -------------------------------------------------------------------
-- face_job_outputs: maps a face studio job -> produced face_profile
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS face_job_outputs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id          uuid NOT NULL REFERENCES studio_jobs(id) ON DELETE CASCADE,
  face_profile_id uuid NOT NULL REFERENCES face_profiles(id) ON DELETE RESTRICT,

  -- Optional: store the asset produced by this job (e.g., best face image)
  output_asset_id uuid REFERENCES media_assets(id) ON DELETE SET NULL,

  created_at      timestamptz NOT NULL DEFAULT now()
);

-- One output mapping per job (Phase-1)
CREATE UNIQUE INDEX IF NOT EXISTS uq_face_job_outputs_job
  ON face_job_outputs (job_id);

CREATE INDEX IF NOT EXISTS idx_face_job_outputs_profile
  ON face_job_outputs (face_profile_id);

CREATE INDEX IF NOT EXISTS idx_face_job_outputs_output_asset
  ON face_job_outputs (output_asset_id);

-- -------------------------------------------------------------------
-- Trigger to enforce job type = 'face'
-- -------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_enforce_face_job_type()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  jt text;
BEGIN
  SELECT studio_type INTO jt FROM studio_jobs WHERE id = NEW.job_id;
  IF jt IS NULL THEN
    RAISE EXCEPTION 'studio_jobs row not found for job_id=%', NEW.job_id;
  END IF;

  IF jt <> 'face' THEN
    RAISE EXCEPTION 'Invalid studio_type for face_job_outputs.job_id=%. Expected face, got %', NEW.job_id, jt;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_face_job_outputs_type ON face_job_outputs;

CREATE TRIGGER trg_face_job_outputs_type
BEFORE INSERT OR UPDATE OF job_id
ON face_job_outputs
FOR EACH ROW
EXECUTE FUNCTION fn_enforce_face_job_type();

COMMIT;