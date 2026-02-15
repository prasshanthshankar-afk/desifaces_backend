create table if not exists public.music_candidates (
  id uuid primary key,
  job_id uuid not null references public.music_video_jobs(id) on delete cascade,
  project_id uuid not null references public.music_projects(id) on delete cascade,
  user_id uuid not null,

  candidate_type text not null
    check (candidate_type in ('lyrics','arrangement','audio','video')),

  group_id uuid not null,              -- one “batch” per stage
  variant_index int not null,          -- 0..N-1 within group
  attempt int not null default 1,       -- for loops/retries

  status text not null
    check (status in ('queued','running','succeeded','failed','discarded','chosen')),

  provider text null,
  seed bigint null,

  -- Text candidates
  content_json jsonb null,             -- lyrics text, arrangement JSON, etc.

  -- Scoring/QC
  score_json jsonb null,

  -- Media candidates
  artifact_id uuid null references public.music_artifacts(id),
  media_asset_id uuid null references public.media_assets(id),
  duration_ms int null,

  meta_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  chosen_at timestamptz null
);

create unique index if not exists music_candidates_uniq
  on public.music_candidates(job_id, candidate_type, group_id, variant_index, attempt);

create index if not exists music_candidates_lookup
  on public.music_candidates(job_id, candidate_type, status);

create index if not exists music_candidates_group
  on public.music_candidates(job_id, candidate_type, group_id);
