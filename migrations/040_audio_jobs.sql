-- migrations/040_audio_jobs.sql
-- Audio/TTS durable entities (audio_clips) + link job outputs to clips
-- Depends on: 020_studio_kernel.sql, 020_media_assets.sql

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -------------------------------------------------------------------
-- audio_clips: reusable audio outputs (TTS now, uploads later)
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audio_clips (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         integer NOT NULL,

  -- tts|upload
  source         text NOT NULL DEFAULT 'tts',

  -- azure|heygen|other
  provider       text NOT NULL DEFAULT 'azure',

  -- BCP-47-ish locale: en-IN, hi-IN, te-IN, ar-SA, fr-FR, etc.
  locale         text,
  voice          text,

  -- For TTS caching: sha256(normalized_script_text)
  text_hash      text,

  -- Underlying stored audio
  media_asset_id uuid NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,

  duration_ms    integer,
  meta_json      jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_audio_clips_source CHECK (source IN ('tts','upload')),
  CONSTRAINT ck_audio_clips_provider_nonempty CHECK (length(trim(provider)) > 0),
  CONSTRAINT ck_audio_clips_duration_nonneg CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX IF NOT EXISTS idx_audio_clips_user_created
  ON audio_clips (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audio_clips_asset
  ON audio_clips (media_asset_id);

CREATE INDEX IF NOT EXISTS idx_audio_clips_provider_locale_voice
  ON audio_clips (provider, locale, voice);

CREATE UNIQUE INDEX IF NOT EXISTS uq_audio_clips_tts_cache
  ON audio_clips (user_id, provider, locale, voice, text_hash)
  WHERE source='tts' AND provider IS NOT NULL AND locale IS NOT NULL AND voice IS NOT NULL AND text_hash IS NOT NULL;

-- -------------------------------------------------------------------
-- audio_job_outputs: maps an audio studio job -> produced audio_clip
-- -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audio_job_outputs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid NOT NULL REFERENCES studio_jobs(id) ON DELETE CASCADE,
  audio_clip_id uuid NOT NULL REFERENCES audio_clips(id) ON DELETE RESTRICT,

  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_audio_job_outputs_job
  ON audio_job_outputs (job_id);

CREATE INDEX IF NOT EXISTS idx_audio_job_outputs_clip
  ON audio_job_outputs (audio_clip_id);

-- -------------------------------------------------------------------
-- Trigger to enforce job type = 'audio'
-- -------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_enforce_audio_job_type()
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

  IF jt <> 'audio' THEN
    RAISE EXCEPTION 'Invalid studio_type for audio_job_outputs.job_id=%. Expected audio, got %', NEW.job_id, jt;
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_audio_job_outputs_type ON audio_job_outputs;

CREATE TRIGGER trg_audio_job_outputs_type
BEFORE INSERT OR UPDATE OF job_id
ON audio_job_outputs
FOR EACH ROW
EXECUTE FUNCTION fn_enforce_audio_job_type();

COMMIT;