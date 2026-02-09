BEGIN;

-- 1) Store reusable style packs / shot recipes / lighting presets
CREATE TABLE IF NOT EXISTS public.music_style_presets (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  tags TEXT[] NOT NULL DEFAULT '{}',
  preset_type TEXT NOT NULL DEFAULT 'style', -- style|shot|lighting|typography|story_arc
  language_hint TEXT NULL,                   -- e.g. 'en', 'hi'
  content JSONB NOT NULL,                    -- structured preset (your plan fragments)
  text_for_embedding TEXT NOT NULL,          -- flattened text used to generate embeddings
  embedding vector(1536) NULL,               -- set after embedding job runs
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_music_style_presets_tags
  ON public.music_style_presets USING GIN (tags);

-- vector index (use HNSW if available; otherwise IVFFLAT)
-- HNSW is preferred when supported by your pgvector build.
DO $$
BEGIN
  -- try HNSW
  BEGIN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_music_style_presets_embedding_hnsw
             ON public.music_style_presets USING hnsw (embedding vector_cosine_ops);';
  EXCEPTION WHEN others THEN
    -- fallback to IVFFLAT
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_music_style_presets_embedding_ivfflat
             ON public.music_style_presets USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);';
  END;
END $$;

-- 2) Store reference-board items per project (optional but very useful)
CREATE TABLE IF NOT EXISTS public.music_project_references (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL,
  ref_type TEXT NOT NULL,                    -- image|video|url|text
  url TEXT NULL,
  title TEXT NULL,
  notes TEXT NULL,
  tags TEXT[] NOT NULL DEFAULT '{}',
  text_for_embedding TEXT NOT NULL DEFAULT '',
  embedding vector(1536) NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_music_project_references_project_id
  ON public.music_project_references (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_music_project_references_tags
  ON public.music_project_references USING GIN (tags);

DO $$
BEGIN
  BEGIN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_music_project_references_embedding_hnsw
             ON public.music_project_references USING hnsw (embedding vector_cosine_ops);';
  EXCEPTION WHEN others THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_music_project_references_embedding_ivfflat
             ON public.music_project_references USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);';
  END;
END $$;

COMMIT;