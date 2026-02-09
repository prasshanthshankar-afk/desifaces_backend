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
  image_asset_id uuid not null, -- references media_assets(id) logically
  voice_mode text not null default 'uploaded'
    check (voice_mode in ('uploaded','generated','none')),
  user_is_owner boolean not null default false,
  created_at timestamptz not null default now(),
  unique(project_id, role)
);

create table if not exists music_tracks (
  id uuid primary key,
  project_id uuid not null references music_projects(id) on delete cascade,
  track_type text not null
    check (track_type in ('song_full','instrumental','vocals_A','vocals_B','audio_master','lyrics_srt')),
  media_asset_id uuid not null,
  duration_ms int null,
  created_at timestamptz not null default now(),
  unique(project_id, track_type)
);

create table if not exists music_alignment (
  project_id uuid primary key references music_projects(id) on delete cascade,
  lyrics_text text null,
  alignment_json jsonb null,      -- word timestamps, speaker tags (optional)
  beat_json jsonb null,           -- bpm, beat_times
  captions_srt_asset_id uuid null, -- media_assets id for .srt
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists music_render_plans (
  project_id uuid primary key references music_projects(id) on delete cascade,
  plan_json jsonb not null,       -- timeline, cuts, b-roll slots, performer visibility
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists music_video_jobs (
  id uuid primary key,
  project_id uuid not null references music_projects(id) on delete cascade,
  status text not null default 'queued'
    check (status in ('queued','running','succeeded','failed')),
  progress int not null default 0 check (progress >= 0 and progress <= 100),
  error text null,

  -- internal artifacts (logical)
  performer_a_video_asset_id uuid null,
  performer_b_video_asset_id uuid null,
  final_video_asset_id uuid null,
  preview_video_asset_id uuid null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_music_jobs_status on music_video_jobs(status, created_at);