begin;

create extension if not exists "pgcrypto";

-- Clean reset (dev-safe): remove broken/old schemas
drop table if exists public.longform_segments cascade;
drop table if exists public.longform_jobs cascade;

-- ----------------------------
-- longform_jobs
-- ----------------------------
create table public.longform_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,

  status text not null default 'queued', -- queued|running|stitching|succeeded|failed
  error_code text null,
  error_message text null,

  face_artifact_id uuid not null,
  aspect_ratio text not null default '9:16',

  segment_seconds int not null default 60,     -- must be <= 120
  max_segment_seconds int not null default 120,

  voice_cfg jsonb not null default '{}'::jsonb, -- { locale, voice, ... }
  tags jsonb not null default '{}'::jsonb,

  script_text text not null,

  total_segments int not null default 0,
  completed_segments int not null default 0,

  final_storage_path text null, -- stable blob path (no querystring)
  final_video_url text null,    -- optional cached SAS (can be re-minted on read)

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint chk_longform_jobs_segment_seconds check (segment_seconds >= 5 and segment_seconds <= 120),
  constraint chk_longform_jobs_max_segment_seconds check (max_segment_seconds >= 5 and max_segment_seconds <= 120),
  constraint chk_longform_jobs_segment_order check (segment_seconds <= max_segment_seconds)
);

create index idx_longform_jobs_user_created
  on public.longform_jobs(user_id, created_at desc);

-- ----------------------------
-- longform_segments
-- ----------------------------
create table public.longform_segments (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.longform_jobs(id) on delete cascade,

  segment_index int not null,
  status text not null default 'queued', -- queued|audio_running|video_running|succeeded|failed
  error_code text null,
  error_message text null,

  text_chunk text not null,
  duration_sec int not null, -- <= 120

  -- svc-audio
  tts_job_id uuid null,
  audio_url text null,         -- SAS mp3 URL
  audio_artifact_id uuid null, -- optional stable id

  -- svc-fusion
  fusion_job_id uuid null,
  provider_job_id text null,
  segment_video_url text null,     -- SAS mp4 URL (short TTL)
  segment_storage_path text null,  -- stable blob path

  -- lock/debug fields
  locked_at timestamptz null,
  locked_by text null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint uq_longform_segments_job_segment unique(job_id, segment_index),
  constraint chk_longform_segments_duration_sec check (duration_sec >= 1 and duration_sec <= 120)
);

create index idx_longform_segments_job_status
  on public.longform_segments(job_id, status);

create index idx_longform_segments_status
  on public.longform_segments(status);

create index idx_longform_segments_queued
  on public.longform_segments(created_at asc)
  where status = 'queued';

create index idx_longform_segments_audio_running
  on public.longform_segments(job_id)
  where status = 'audio_running';

create index idx_longform_segments_video_running
  on public.longform_segments(job_id)
  where status = 'video_running';

create index idx_longform_segments_inflight
  on public.longform_segments(job_id, status)
  where status in ('audio_running','video_running');

-- ----------------------------
-- updated_at touch triggers
-- ----------------------------
create or replace function public.fn_touch_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger trg_longform_jobs_touch
before update on public.longform_jobs
for each row execute function public.fn_touch_updated_at();

create trigger trg_longform_segments_touch
before update on public.longform_segments
for each row execute function public.fn_touch_updated_at();

alter table public.longform_jobs
  add column if not exists auth_token text;

commit;