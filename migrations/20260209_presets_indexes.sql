-- Tags filter
create index if not exists music_style_presets_tags_gin
on public.music_style_presets
using gin (tags);

-- Type filter
create index if not exists music_style_presets_preset_type_idx
on public.music_style_presets (preset_type);

-- Vector index (pick one depending on pgvector support)
-- Option A: IVFFLAT (common)
-- NOTE: you need to choose list size; 100 is a reasonable start.
create index if not exists music_style_presets_embedding_ivfflat
on public.music_style_presets
using ivfflat (embedding vector_cosine_ops)
with (lists = 100);

-- Option B: HNSW (if enabled in your pgvector version)
-- create index if not exists music_style_presets_embedding_hnsw
-- on public.music_style_presets
-- using hnsw (embedding vector_cosine_ops);