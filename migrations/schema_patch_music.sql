create table if not exists music_artifacts (
  id uuid primary key,
  user_id uuid not null,
  project_id uuid null,
  job_id uuid null,
  kind text not null,          -- e.g. track:song_full, video:performer_A, video:final_9_16
  storage_path text not null,  -- blob path in MUSIC_OUTPUT_CONTAINER
  content_type text not null,
  bytes bigint not null,
  sha256 text not null,
  created_at timestamptz not null default now()
);

create index if not exists idx_music_artifacts_project on music_artifacts(project_id, created_at);
create index if not exists idx_music_artifacts_job on music_artifacts(job_id, created_at);

-- === core music domain ===
create table if not exists music_projects (
  id uuid primary key,
  user_id uuid not null,
  title text not null default 'Untitled Music Video',
  mode text not null check (mode in ('single','duet')),
  duet_layout text not null default 'split_screen'
    check (duet_layout in ('split_screen','alternating','same_stage')),
  language_hint text null,
  scene_pack_id text null,
  camera_edit text not null default 'beat_cut' check (camera_edit in ('smooth','beat_cut','aggressive')),
  band_pack text[] not null default array[]::text[],
  status text not null default 'draft'
    check (status in ('draft','planning','ready','rendering','succeeded','failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists music_performers (
  id uuid primary key,
  project_id uuid not null references music_projects(id) on delete cascade,
  role text not null check (role in ('A','B')),
  image_asset_id uuid null,     -- optional (if you later resolve via shared media_assets)
  image_url text null,          -- store Face Studio SAS URL now (works immediately)
  user_is_owner boolean not null default false,
  created_at timestamptz not null default now(),
  unique(project_id, role)
);

create table if not exists music_tracks (
  id uuid primary key,
  project_id uuid not null references music_projects(id) on delete cascade,
  track_type text not null
    check (track_type in ('song_full','instrumental','vocals_A','vocals_B','audio_master','lyrics_srt')),
  artifact_id uuid not null references music_artifacts(id) on delete restrict,
  duration_ms int null,
  created_at timestamptz not null default now(),
  unique(project_id, track_type)
);

create table if not exists music_alignment (
  project_id uuid primary key references music_projects(id) on delete cascade,
  lyrics_text text null,
  alignment_json jsonb null,
  beat_json jsonb null,
  captions_srt_artifact_id uuid null references music_artifacts(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists music_render_plans (
  project_id uuid primary key references music_projects(id) on delete cascade,
  plan_json jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists music_video_jobs (
  id uuid primary key,
  project_id uuid not null references music_projects(id) on delete cascade,
  status text not null default 'queued'
    check (status in ('queued','running','succeeded','failed')),
  progress int not null default 0 check (progress between 0 and 100),
  error text null,

  preview_video_artifact_id uuid null references music_artifacts(id),
  final_video_artifact_id uuid null references music_artifacts(id),
  performer_a_video_artifact_id uuid null references music_artifacts(id),
  performer_b_video_artifact_id uuid null references music_artifacts(id),

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_music_jobs_status on music_video_jobs(status, created_at);

ALTER TABLE public.music_tracks
  ADD COLUMN IF NOT EXISTS artifact_id uuid;

ALTER TABLE public.music_tracks
  ADD COLUMN IF NOT EXISTS duration_ms integer NOT NULL DEFAULT 0;

-- If duration_ms existed but was nullable, enforce NOT NULL safely
UPDATE public.music_tracks SET duration_ms = 0 WHERE duration_ms IS NULL;
ALTER TABLE public.music_tracks ALTER COLUMN duration_ms SET NOT NULL;

-- (Optional but recommended) ensure one row per (project_id, track_type)
CREATE UNIQUE INDEX IF NOT EXISTS music_tracks_project_track_type_uidx
  ON public.music_tracks(project_id, track_type);

ALTER TABLE public.music_tracks
  ADD COLUMN IF NOT EXISTS artifact_id uuid;

ALTER TABLE public.music_tracks
  ALTER COLUMN media_asset_id DROP NOT NULL;

alter table public.music_performers
  add column if not exists image_url text;

alter table public.music_performers
  add column if not exists updated_at timestamptz not null default now();

-- backfill updated_at for existing rows (if any)
update public.music_performers set updated_at = now() where updated_at is null;