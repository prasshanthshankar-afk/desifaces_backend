-- migrations/020_studio_kernel.sql
-- Core job orchestration kernel for Face/Audio/Fusion (Digital Performance)

BEGIN;

-- Extensions (safe)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ------------------------------------------------------------
-- studio_jobs: one row per job (face/audio/fusion)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS studio_jobs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  studio_type  text NOT NULL, -- 'face' | 'audio' | 'fusion'
  status       text NOT NULL DEFAULT 'queued', -- queued|running|succeeded|failed
  user_id      integer NOT NULL,
  request_hash text NOT NULL, -- deterministic hash of stable spec
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  meta_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_code   text,
  error_message text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_studio_jobs_type_status_created
  ON studio_jobs (studio_type, status, created_at);

CREATE INDEX IF NOT EXISTS idx_studio_jobs_user_created
  ON studio_jobs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_studio_jobs_request_hash
  ON studio_jobs (request_hash);

-- (Optional) if you want strict idempotent create-job at DB level:
-- One active job per (user_id, studio_type, request_hash).
-- Comment out if you want multiple runs for same hash.
CREATE UNIQUE INDEX IF NOT EXISTS uq_studio_jobs_user_type_hash
  ON studio_jobs (user_id, studio_type, request_hash);

-- ------------------------------------------------------------
-- studio_job_steps: step-level state tracking (auditable)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS studio_job_steps (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id        uuid NOT NULL REFERENCES studio_jobs(id) ON DELETE CASCADE,
  step_code     text NOT NULL, -- e.g. TTS_SYNTH | PROVIDER_SUBMIT | PROVIDER_POLL | FINALIZE
  status        text NOT NULL DEFAULT 'queued', -- queued|running|succeeded|failed
  attempt       integer NOT NULL DEFAULT 0,
  error_code    text,
  error_message text,
  meta_json     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per job+step_code (so repos can UPSERT)
CREATE UNIQUE INDEX IF NOT EXISTS uq_studio_job_steps_job_step
  ON studio_job_steps (job_id, step_code);

CREATE INDEX IF NOT EXISTS idx_studio_job_steps_job
  ON studio_job_steps (job_id, created_at);

-- ------------------------------------------------------------
-- provider_runs: repeatable external-provider submission tracking
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_runs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id          uuid NOT NULL REFERENCES studio_jobs(id) ON DELETE CASCADE,
  provider        text NOT NULL, -- 'heygen_av4' | 'native' etc
  idempotency_key text NOT NULL, -- provider:payload_version:request_hash
  provider_job_id text,          -- e.g. HeyGen video_id
  provider_status text NOT NULL DEFAULT 'created', -- created|submitted|processing|succeeded|failed
  request_json    jsonb NOT NULL DEFAULT '{}'::jsonb, -- exact payload sent
  response_json   jsonb NOT NULL DEFAULT '{}'::jsonb, -- submit response
  meta_json       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_runs_idempotency
  ON provider_runs (idempotency_key);

CREATE INDEX IF NOT EXISTS idx_provider_runs_job
  ON provider_runs (job_id, created_at);

CREATE INDEX IF NOT EXISTS idx_provider_runs_provider_job_id
  ON provider_runs (provider, provider_job_id);

-- ------------------------------------------------------------
-- artifacts: everything produced/consumed (face image, audio, video, share url)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id       uuid NOT NULL REFERENCES studio_jobs(id) ON DELETE CASCADE,
  kind         text NOT NULL, -- face_image|audio|video|share_url|thumb|debug_payload
  url          text NOT NULL, -- az://... or https://...
  content_type text,
  sha256       text,
  bytes        bigint,
  meta_json    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_job
  ON artifacts (job_id, created_at);

CREATE INDEX IF NOT EXISTS idx_artifacts_kind
  ON artifacts (kind);

COMMIT;