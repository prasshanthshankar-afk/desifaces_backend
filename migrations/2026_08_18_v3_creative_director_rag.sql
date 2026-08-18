-- V3-MPS1B: Creative Director RAG knowledge and retrieval audit.
-- Product creative knowledge is separate from EIP engineering knowledge.
-- Existing creation/project/story context remains in canonical V3 domain tables.

BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.v3_creative_knowledge_sources (
  source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type text NOT NULL,
  source_key text NOT NULL,
  title text NOT NULL,
  locale text,
  revision text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_creative_source_type_nonempty CHECK (length(btrim(source_type)) > 0),
  CONSTRAINT ck_v3_creative_source_key_nonempty CHECK (length(btrim(source_key)) > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v3_creative_source_revision
  ON public.v3_creative_knowledge_sources(source_type, source_key, coalesce(revision, ''));

CREATE TABLE IF NOT EXISTS public.v3_creative_knowledge_chunks (
  chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id uuid NOT NULL REFERENCES public.v3_creative_knowledge_sources(source_id) ON DELETE CASCADE,
  sequence_no integer NOT NULL DEFAULT 0,
  content text NOT NULL,
  locale text,
  tags text[] NOT NULL DEFAULT '{}'::text[],
  embedding vector,
  embedding_model text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_id, sequence_no),
  CONSTRAINT ck_v3_creative_chunk_sequence CHECK (sequence_no >= 0),
  CONSTRAINT ck_v3_creative_chunk_content CHECK (length(btrim(content)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_v3_creative_chunks_source_active
  ON public.v3_creative_knowledge_chunks(source_id, is_active, sequence_no);
CREATE INDEX IF NOT EXISTS idx_v3_creative_chunks_tags
  ON public.v3_creative_knowledge_chunks USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_v3_creative_chunks_fts
  ON public.v3_creative_knowledge_chunks
  USING gin(to_tsvector('simple', content));

CREATE TABLE IF NOT EXISTS public.v3_director_retrieval_events (
  retrieval_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE RESTRICT,
  project_id uuid,
  story_id uuid,
  thread_id text,
  query_text text NOT NULL,
  source_refs text[] NOT NULL DEFAULT '{}'::text[],
  result_count integer NOT NULL DEFAULT 0,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_director_retrieval_result_count CHECK (result_count >= 0)
);
CREATE INDEX IF NOT EXISTS idx_v3_director_retrieval_account_created
  ON public.v3_director_retrieval_events(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_director_retrieval_story_created
  ON public.v3_director_retrieval_events(story_id, created_at DESC)
  WHERE story_id IS NOT NULL;

COMMIT;
