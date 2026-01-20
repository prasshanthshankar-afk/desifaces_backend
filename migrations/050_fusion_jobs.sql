-- migrations/050_fusion_jobs.sql
-- Fusion / Digital Performance durable entities + link job outputs
-- Depends on: 020_studio_kernel.sql, 020_media_assets.sql, (optionally) 030_face_jobs.sql, 040_audio_jobs.sql

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -------------------------------------------------------------------
-- digital_performances: the durable end-user object (final video)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS digital_performances (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           integer NOT NULL,

  -- Optional links to reusable inputs
  face_profile_id   uuid REFERENCES face_profiles(id) ON DELETE SET NULL,
  audio_clip_id     uuid REFERENCES audio_clips(id) ON DELETE SET NULL,

  -- Durable stored output video (copy provider output into blob and register as media_asset)
  video_asset_id    uuid REFERENCES media_assets(id) ON DELETE SET NULL,

  -- Optional share page URL (HeyGen / your own)
  share_url         text,

  -- Provider info
  provider          text NOT NULL DEFAULT 'heygen_av4',
  provider_job_id   text,  -- HeyGen video_id

  -- processing|ready|failed
  status            text NOT NULL DEFAULT 'processing',

  meta_json         jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_digital_performances_status CHECK (status IN ('processing','ready','failed')),
  CONSTRAINT ck_digital_performances_provider_nonempty CHECK (length(trim(provider)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_digital_performances_user_created
  ON digital_performances (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_digital_performances_user_status
  ON digital_performances (user_id, status);

CREATE INDEX IF NOT EXISTS idx_digital_performances_face
  ON digital_performances (face_profile_id);

CREATE INDEX IF NOT EXISTS idx_digital_performances_audio
  ON digital_performances (audio_clip_id);

CREATE INDEX IF NOT EXISTS idx_digital_performances_video_asset
  ON digital_performances (video_asset_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_digital_performances_provider_job
  ON digital_performances (provider, provider_job_id)
  WHERE provider_job_id IS NOT NULL;

-- -------------------------------------------------------------------
-- fusion_job_outputs: maps a fusion job -> produced digital_performance
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fusion_job_outputs (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id                 uuid NOT NULL REFERENCES studio_jobs(id) ON DELETE CASCADE,
  digital_performance_id uuid NOT NULL REFERENCES digital_performances(id) ON DELETE RESTRICT,

  created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_fusion_job_outputs_job
  ON fusion_job_outputs (job_id);

CREATE INDEX IF NOT EXISTS idx_fusion_job_outputs_perf
  ON fusion_job_outputs (digital_performance_id);

-- -------------------------------------------------------------------
-- Trigger to enforce job type = 'fusion'
-- -------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_enforce_fusion_job_type()
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

  IF jt <> 'fusion' THEN
    RAISE EXCEPTION 'Invalid studio_type for fusion_job_outputs.job_id=%. Expected fusion, got %', NEW.job_id, jt;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_fusion_job_outputs_type ON fusion_job_outputs;

CREATE TRIGGER trg_fusion_job_outputs_type
BEFORE INSERT OR UPDATE OF job_id
ON fusion_job_outputs
FOR EACH ROW
EXECUTE FUNCTION fn_enforce_fusion_job_type();

COMMIT;