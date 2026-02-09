create table if not exists music_compose_jobs (
  id uuid primary key,
  user_id uuid not null,

  status text not null default 'queued' check (status in ('queued','running','succeeded','failed')),
  progress int not null default 0 check (progress between 0 and 100),

  project_id uuid null,
  source_job_id uuid null, -- svc-music job_id for correlation

  auth_token text null, -- service bearer for workers (never store user JWT)

  performer_videos jsonb not null, -- {"A": "...", "B": "..."}  (SAS urls)
  audio_master_url text not null,  -- SAS url
  exports text[] not null default array['9:16']::text[],
  burn_captions boolean not null default true,
  camera_edit text not null default 'beat_cut',
  band_pack text[] not null default array[]::text[],

  preview_storage_path text null,
  outputs_storage_paths jsonb null, -- {"9:16":"path.mp4","16:9":"..."} etc

  error_code text null,
  error_message text null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_music_compose_jobs_user on music_compose_jobs(user_id, created_at desc);
create index if not exists idx_music_compose_jobs_status on music_compose_jobs(status, created_at);
