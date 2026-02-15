create table if not exists public.music_candidates (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.music_video_jobs(id) on delete cascade,
  project_id uuid not null references public.music_projects(id) on delete cascade,
  user_id uuid not null,

  candidate_type text not null,
  group_id uuid not null,
  variant_index int not null,
  attempt int not null default 1,
  status text not null default 'queued',

  provider text null,
  seed bigint null,

  content_json jsonb null,
  score_json jsonb null,

  artifact_id uuid null references public.music_artifacts(id),
  media_asset_id uuid null references public.media_assets(id),
  duration_ms int null,

  meta_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  chosen_at timestamptz null
);

-- 2) Ensure core columns exist (no-op if already there)
alter table public.music_candidates add column if not exists project_id uuid;
alter table public.music_candidates add column if not exists user_id uuid;
alter table public.music_candidates add column if not exists candidate_type text;
alter table public.music_candidates add column if not exists group_id uuid;
alter table public.music_candidates add column if not exists variant_index int;
alter table public.music_candidates add column if not exists attempt int;
alter table public.music_candidates add column if not exists status text;
alter table public.music_candidates add column if not exists provider text;
alter table public.music_candidates add column if not exists seed bigint;
alter table public.music_candidates add column if not exists content_json jsonb;
alter table public.music_candidates add column if not exists score_json jsonb;
alter table public.music_candidates add column if not exists artifact_id uuid;
alter table public.music_candidates add column if not exists media_asset_id uuid;
alter table public.music_candidates add column if not exists duration_ms int;
alter table public.music_candidates add column if not exists meta_json jsonb;
alter table public.music_candidates add column if not exists chosen_at timestamptz;

-- 3) Indexes for frontend listing + controller fan-in
create unique index if not exists ux_music_candidates_group_variant_attempt
  on public.music_candidates(job_id, candidate_type, group_id, variant_index, attempt);

create index if not exists idx_music_candidates_job_type_status
  on public.music_candidates(job_id, candidate_type, status);

create index if not exists idx_music_candidates_job_type_group
  on public.music_candidates(job_id, candidate_type, group_id);


begin;

-- Candidate identity + grouping
alter table public.music_candidates
  add column if not exists candidate_type text;

alter table public.music_candidates
  add column if not exists group_id uuid;

alter table public.music_candidates
  add column if not exists variant_index integer;

alter table public.music_candidates
  add column if not exists attempt integer default 1;

-- Status + provider metadata
alter table public.music_candidates
  add column if not exists status text;

alter table public.music_candidates
  add column if not exists provider text;

alter table public.music_candidates
  add column if not exists seed bigint;

-- Payloads
alter table public.music_candidates
  add column if not exists content_json jsonb;

alter table public.music_candidates
  add column if not exists score_json jsonb;

alter table public.music_candidates
  add column if not exists meta_json jsonb not null default '{}'::jsonb;

-- Output refs
alter table public.music_candidates
  add column if not exists artifact_id uuid;

alter table public.music_candidates
  add column if not exists media_asset_id uuid;

alter table public.music_candidates
  add column if not exists duration_ms integer;

-- Bookkeeping
alter table public.music_candidates
  add column if not exists chosen_at timestamptz;

-- Optional: link back to provider_runs for traceability
alter table public.music_candidates
  add column if not exists provider_run_id uuid;

-- Indexes for fast UX/status
create index if not exists idx_music_candidates_job_type_status
  on public.music_candidates(job_id, candidate_type, status);

create index if not exists idx_music_candidates_job_type_group
  on public.music_candidates(job_id, candidate_type, group_id);

create unique index if not exists ux_music_candidates_group_variant_attempt
  on public.music_candidates(job_id, candidate_type, group_id, variant_index, attempt);

commit;