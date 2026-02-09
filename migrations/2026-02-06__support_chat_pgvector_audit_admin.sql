-- 2026-02-06__support_chat_pgvector_audit_admin__upgrade.sql
-- Upgrades existing support_sessions/support_events to auditable, admin-queryable, tamper-evident ledger.
-- Safe to run multiple times.

BEGIN;

-- ----------------------------
-- Extensions
-- ----------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ----------------------------
-- Optional: minimal admin table
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.admin_users (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  email        text NOT NULL UNIQUE,
  display_name text NULL,
  role         text NOT NULL DEFAULT 'admin',
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------
-- Ensure support_sessions exists (keep your existing schema)
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.support_sessions (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id      uuid NOT NULL,
  project_id   uuid NOT NULL,
  job_id       uuid NULL,
  surface      text NOT NULL,
  status       text NOT NULL DEFAULT 'open',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS support_sessions_user_created_idx
  ON public.support_sessions(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS support_sessions_project_idx
  ON public.support_sessions(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS support_sessions_job_idx
  ON public.support_sessions(job_id, created_at DESC)
  WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS support_sessions_surface_idx
  ON public.support_sessions(surface, created_at DESC);

-- updated_at trigger helper
CREATE OR REPLACE FUNCTION public._touch_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_support_sessions_touch_updated_at') THEN
    CREATE TRIGGER trg_support_sessions_touch_updated_at
    BEFORE UPDATE ON public.support_sessions
    FOR EACH ROW
    EXECUTE FUNCTION public._touch_updated_at();
  END IF;
END $$;

-- ----------------------------
-- Ensure support_events exists (the older version may already exist)
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.support_events (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  session_id   uuid NOT NULL REFERENCES public.support_sessions(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL,
  kind         text NOT NULL,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------
-- Upgrade support_events in-place (ADD missing columns)
-- ----------------------------
ALTER TABLE public.support_events
  ADD COLUMN IF NOT EXISTS actor_type           text NULL,
  ADD COLUMN IF NOT EXISTS actor_user_id        uuid NULL,
  ADD COLUMN IF NOT EXISTS actor_admin_id       uuid NULL,
  ADD COLUMN IF NOT EXISTS impersonated_user_id uuid NULL,
  ADD COLUMN IF NOT EXISTS request_id           text NULL,
  ADD COLUMN IF NOT EXISTS ip                   inet NULL,
  ADD COLUMN IF NOT EXISTS user_agent           text NULL,
  ADD COLUMN IF NOT EXISTS retention_until      timestamptz NULL,
  ADD COLUMN IF NOT EXISTS prev_hash            bytea NULL,
  ADD COLUMN IF NOT EXISTS event_hash           bytea NULL;

-- Backfill actor columns from legacy user_id if needed
UPDATE public.support_events
SET
  actor_type    = COALESCE(actor_type, 'user'),
  actor_user_id = COALESCE(actor_user_id, user_id)
WHERE actor_type IS NULL OR actor_user_id IS NULL;

-- Make actor_type NOT NULL (only if column exists + currently nullable)
DO $$
BEGIN
  -- Only attempt if the constraint isn't already there and column exists
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='support_events' AND column_name='actor_type'
  ) THEN
    -- Ensure no nulls remain (safety)
    UPDATE public.support_events SET actor_type='user' WHERE actor_type IS NULL;
    ALTER TABLE public.support_events ALTER COLUMN actor_type SET NOT NULL;
  END IF;
END $$;

-- event_hash must be NOT NULL eventually; we will compute/backfill first, then enforce.
-- (We can't set NOT NULL until every row has a value.)

-- ----------------------------
-- Immutability triggers (append-only)
-- ----------------------------
CREATE OR REPLACE FUNCTION public._support_events_immutable()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'support_events is append-only (no % allowed)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_support_events_no_update') THEN
    CREATE TRIGGER trg_support_events_no_update
    BEFORE UPDATE ON public.support_events
    FOR EACH ROW EXECUTE FUNCTION public._support_events_immutable();
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_support_events_no_delete') THEN
    CREATE TRIGGER trg_support_events_no_delete
    BEFORE DELETE ON public.support_events
    FOR EACH ROW EXECUTE FUNCTION public._support_events_immutable();
  END IF;
END $$;

-- ----------------------------
-- Hash chain function + trigger
-- NOTE: Works for new inserts. Existing rows are backfilled below.
-- ----------------------------
CREATE OR REPLACE FUNCTION public._support_events_hash_chain()
RETURNS trigger AS $$
DECLARE
  v_prev bytea;
  v_data text;
BEGIN
  SELECT e.event_hash INTO v_prev
  FROM public.support_events e
  WHERE e.session_id = NEW.session_id
    AND e.event_hash IS NOT NULL
  ORDER BY e.created_at DESC, e.id DESC
  LIMIT 1;

  NEW.prev_hash = v_prev;

  v_data :=
      coalesce(encode(NEW.prev_hash, 'hex'), '') || '|' ||
      coalesce(NEW.session_id::text, '') || '|' ||
      coalesce(NEW.actor_type, '') || '|' ||
      coalesce(NEW.actor_user_id::text, '') || '|' ||
      coalesce(NEW.actor_admin_id::text, '') || '|' ||
      coalesce(NEW.impersonated_user_id::text, '') || '|' ||
      coalesce(NEW.kind, '') || '|' ||
      coalesce(NEW.request_id, '') || '|' ||
      coalesce(NEW.ip::text, '') || '|' ||
      coalesce(NEW.user_agent, '') || '|' ||
      coalesce(NEW.retention_until::text, '') || '|' ||
      coalesce(NEW.payload::text, '') || '|' ||
      coalesce(NEW.created_at::text, '');

  NEW.event_hash = digest(v_data, 'sha256');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_support_events_hash_chain') THEN
    CREATE TRIGGER trg_support_events_hash_chain
    BEFORE INSERT ON public.support_events
    FOR EACH ROW EXECUTE FUNCTION public._support_events_hash_chain();
  END IF;
END $$;

-- ----------------------------
-- Backfill hash chain for existing rows (per session)
-- This runs only for rows missing event_hash.
-- ----------------------------
DO $$
DECLARE
  r record;
  e record;
  v_prev bytea;
  v_data text;
  v_hash bytea;
BEGIN
  FOR r IN
    SELECT DISTINCT session_id
    FROM public.support_events
    WHERE event_hash IS NULL
  LOOP
    v_prev := NULL;

    FOR e IN
      SELECT id, session_id, actor_type, actor_user_id, actor_admin_id, impersonated_user_id,
             kind, payload, request_id, ip, user_agent, retention_until, created_at
      FROM public.support_events
      WHERE session_id = r.session_id
      ORDER BY created_at ASC, id ASC
    LOOP
      v_data :=
          coalesce(encode(v_prev, 'hex'), '') || '|' ||
          coalesce(e.session_id::text, '') || '|' ||
          coalesce(e.actor_type, '') || '|' ||
          coalesce(e.actor_user_id::text, '') || '|' ||
          coalesce(e.actor_admin_id::text, '') || '|' ||
          coalesce(e.impersonated_user_id::text, '') || '|' ||
          coalesce(e.kind, '') || '|' ||
          coalesce(e.request_id, '') || '|' ||
          coalesce(e.ip::text, '') || '|' ||
          coalesce(e.user_agent, '') || '|' ||
          coalesce(e.retention_until::text, '') || '|' ||
          coalesce(e.payload::text, '') || '|' ||
          coalesce(e.created_at::text, '');

      v_hash := digest(v_data, 'sha256');

      UPDATE public.support_events
      SET prev_hash = v_prev,
          event_hash = v_hash
      WHERE id = e.id;

      v_prev := v_hash;
    END LOOP;
  END LOOP;
END $$;

-- Enforce event_hash NOT NULL now that it's backfilled
DO $$
BEGIN
  UPDATE public.support_events
  SET event_hash = digest('backfill|' || id::text, 'sha256')
  WHERE event_hash IS NULL;

  ALTER TABLE public.support_events
    ALTER COLUMN event_hash SET NOT NULL;
END $$;

-- ----------------------------
-- Indexes (now safe because columns exist)
-- ----------------------------
CREATE INDEX IF NOT EXISTS support_events_session_created_idx
  ON public.support_events(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS support_events_actor_user_idx
  ON public.support_events(actor_user_id, created_at DESC)
  WHERE actor_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS support_events_actor_admin_idx
  ON public.support_events(actor_admin_id, created_at DESC)
  WHERE actor_admin_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS support_events_kind_created_idx
  ON public.support_events(kind, created_at DESC);

CREATE INDEX IF NOT EXISTS support_events_request_id_idx
  ON public.support_events(request_id)
  WHERE request_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS support_events_ip_idx
  ON public.support_events(ip)
  WHERE ip IS NOT NULL;

CREATE INDEX IF NOT EXISTS support_events_payload_gin_idx
  ON public.support_events USING GIN (payload);

-- ----------------------------
-- Admin view
-- ----------------------------
CREATE OR REPLACE VIEW public.v_support_events_admin AS
SELECT
  e.id,
  e.session_id,
  s.user_id          AS session_user_id,
  s.project_id,
  s.job_id,
  s.surface,
  s.status           AS session_status,

  e.actor_type,
  e.actor_user_id,
  e.actor_admin_id,
  e.impersonated_user_id,

  e.kind,
  e.payload,

  e.request_id,
  e.ip,
  e.user_agent,

  e.retention_until,
  encode(e.prev_hash, 'hex')  AS prev_hash_hex,
  encode(e.event_hash, 'hex') AS event_hash_hex,

  e.created_at
FROM public.support_events e
JOIN public.support_sessions s ON s.id = e.session_id;

-- ----------------------------
-- pgvector KB tables (same as before; safe)
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.support_kb_docs (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  domain       text NOT NULL,
  doc_type     text NOT NULL,
  title        text NOT NULL,
  body         text NOT NULL,
  tags         text[] NOT NULL DEFAULT '{}',
  source       text NULL,
  embedding    vector(1536) NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS support_kb_docs_domain_idx
  ON public.support_kb_docs(domain);

CREATE INDEX IF NOT EXISTS support_kb_docs_doc_type_idx
  ON public.support_kb_docs(doc_type);

CREATE INDEX IF NOT EXISTS support_kb_docs_tags_gin_idx
  ON public.support_kb_docs USING GIN(tags);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_support_kb_docs_touch_updated_at') THEN
    CREATE TRIGGER trg_support_kb_docs_touch_updated_at
    BEFORE UPDATE ON public.support_kb_docs
    FOR EACH ROW
    EXECUTE FUNCTION public._touch_updated_at();
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = 'support_kb_docs_embedding_idx'
      AND n.nspname = 'public'
  ) THEN
    EXECUTE 'CREATE INDEX support_kb_docs_embedding_idx
             ON public.support_kb_docs
             USING ivfflat (embedding vector_cosine_ops)
             WITH (lists = 100)';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.support_incidents (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  domain       text NOT NULL,
  fingerprint  text NOT NULL,
  summary      text NOT NULL,
  resolution   text NOT NULL,
  severity     int  NOT NULL DEFAULT 2,
  embedding    vector(1536) NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS support_incidents_domain_fingerprint_uq
  ON public.support_incidents(domain, fingerprint);

CREATE INDEX IF NOT EXISTS support_incidents_domain_severity_idx
  ON public.support_incidents(domain, severity DESC, created_at DESC);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = 'support_incidents_embedding_idx'
      AND n.nspname = 'public'
  ) THEN
    EXECUTE 'CREATE INDEX support_incidents_embedding_idx
             ON public.support_incidents
             USING ivfflat (embedding vector_cosine_ops)
             WITH (lists = 100)';
  END IF;
END $$;

COMMIT;