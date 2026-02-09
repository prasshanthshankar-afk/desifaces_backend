create table if not exists public.longform_jobs (
  id uuid primary key,
  user_id uuid not null,
  status text not null,
  provider text not null default 'heygen_av4',
  image_ref text not null,
  voice_config_json jsonb not null default '{}'::jsonb,
  segment_seconds int not null default 150,
  max_segment_seconds int not null default 180,
  output_resolution text not null default '1080p',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  segments_total int not null default 0,
  segments_done int not null default 0,

  final_storage_path text null,
  final_video_url text null,
  last_error text null
);

create index if not exists idx_longform_jobs_user_updated
  on public.longform_jobs(user_id, updated_at desc);

create index if not exists idx_longform_jobs_status
  on public.longform_jobs(status, updated_at desc);

-- public.longform_segments
create table if not exists public.longform_segments (
  id uuid primary key,
  longform_job_id uuid not null references public.longform_jobs(id) on delete cascade,
  segment_index int not null,
  status text not null,
  attempt_count int not null default 0,

  script_text text not null,
  tts_job_id text null,
  audio_storage_path text null,
  audio_url text null,

  fusion_job_id text null,
  video_storage_path text null,
  video_url text null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  last_error text null
);

create unique index if not exists uq_longform_segments_job_idx
  on public.longform_segments(longform_job_id, segment_index);

create index if not exists idx_longform_segments_status
  on public.longform_segments(status, updated_at desc);