BEGIN;

-- ----------------------------
-- Extensions
-- ----------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ----------------------------
-- Enum-like CHECKs (keep simple; your app enforces strict enums)
-- ----------------------------
-- Notes:
-- - We avoid Postgres ENUM types to keep migrations flexible.
-- - If you prefer ENUMs, we can convert later.

-- ----------------------------
-- Support sessions: one "thread" per project/job/surface
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.support_sessions (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id      uuid NOT NULL,
  project_id   uuid NOT NULL,
  job_id       uuid NULL,
  surface      text NOT NULL,        -- e.g. 'music_studio'
  status       text NOT NULL DEFAULT 'open',  -- 'open'|'closed'
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

-- ----------------------------
-- Support events: snapshots, actions, user messages (auditable)
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.support_events (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  session_id   uuid NOT NULL REFERENCES public.support_sessions(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL,
  kind         text NOT NULL,     -- 'snapshot'|'action'|'user_message'|'system'
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS support_events_session_created_idx
  ON public.support_events(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS support_events_user_created_idx
  ON public.support_events(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS support_events_kind_created_idx
  ON public.support_events(kind, created_at DESC);

-- ----------------------------
-- Optional: helper for updated_at
-- ----------------------------
CREATE OR REPLACE FUNCTION public._touch_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'trg_support_sessions_touch_updated_at'
  ) THEN
    CREATE TRIGGER trg_support_sessions_touch_updated_at
    BEFORE UPDATE ON public.support_sessions
    FOR EACH ROW
    EXECUTE FUNCTION public._touch_updated_at();
  END IF;
END $$;

-- ----------------------------
-- Knowledge base (pgvector): runbooks/faq/incidents
-- ----------------------------
-- IMPORTANT:
-- Choose the embedding dimension to match your embedding model.
-- 1536 is common for many embedding models; adjust if needed (e.g., 3072, etc.)
-- If you change dimension later, you'll need a migration.

CREATE TABLE IF NOT EXISTS public.support_kb_docs (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  domain       text NOT NULL,         -- e.g. 'music'
  doc_type     text NOT NULL,         -- 'runbook'|'faq'|'incident'
  title        text NOT NULL,
  body         text NOT NULL,
  tags         text[] NOT NULL DEFAULT '{}',
  source       text NULL,             -- 'internal', 'ops', etc.
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
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'trg_support_kb_docs_touch_updated_at'
  ) THEN
    CREATE TRIGGER trg_support_kb_docs_touch_updated_at
    BEFORE UPDATE ON public.support_kb_docs
    FOR EACH ROW
    EXECUTE FUNCTION public._touch_updated_at();
  END IF;
END $$;

-- Vector index: IVFFLAT requires ANALYZE and works best with enough rows.
-- Safe to create; will be used when you start storing embeddings.
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

-- ----------------------------
-- Similar incidents table (pgvector): "known issues" by fingerprint
-- ----------------------------
CREATE TABLE IF NOT EXISTS public.support_incidents (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  domain       text NOT NULL,           -- e.g. 'music'
  fingerprint  text NOT NULL,           -- e.g. 'error_missing_full_mix_ref'
  summary      text NOT NULL,
  resolution   text NOT NULL,
  severity     int  NOT NULL DEFAULT 2, -- 1..5
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

-- ----------------------------
-- Optional: seed a few KB docs (safe; insert only if empty)
-- ----------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.support_kb_docs LIMIT 1) THEN
    INSERT INTO public.support_kb_docs(domain, doc_type, title, body, tags, source)
    VALUES
      (
        'music',
        'faq',
        'Lyrics vs Karaoke Captions (Simple)',
        'Lyrics are the words of the song. Karaoke captions are lyrics timed to the music so they can appear on-screen in sync. You can publish without karaoke captions; they are optional.',
        ARRAY['lyrics','captions','karaoke','ux'],
        'internal'
      ),
      (
        'music',
        'runbook',
        'Publish blocked: missing full mix',
        'If publish fails due to missing full mix: verify a full_mix track exists in music_tracks for the project. If missing, retry generate_audio step or re-run the job. Ensure audio URL is present in track meta_json.url.',
        ARRAY['publish','full_mix','tracks','troubleshooting'],
        'internal'
      );
  END IF;
END $$;

COMMIT;