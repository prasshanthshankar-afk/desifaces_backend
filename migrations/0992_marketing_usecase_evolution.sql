-- services/svc-marketing/app/app/db/migrations/003_usecase_evolution.sql
-- Adds:
--  - use case lifecycle: approved/source/version/parent/usage stats
--  - platform posts mapping (run -> instagram media_id)
--  - post metrics table (daily snapshots)
--  - optional aggregated fields on use cases

create extension if not exists pgcrypto;

-- ---- marketing_use_cases evolution fields ----
alter table marketing_use_cases
  add column if not exists approved boolean not null default false,
  add column if not exists source text not null default 'seed', -- seed|llm_curated|human_curated
  add column if not exists version int not null default 1,
  add column if not exists parent_use_case_id uuid null references marketing_use_cases(use_case_id),
  add column if not exists created_by uuid null,
  add column if not exists updated_by uuid null,
  add column if not exists last_used_at timestamptz null,
  add column if not exists usage_count int not null default 0,
  add column if not exists last_metrics_json jsonb not null default '{}'::jsonb;

-- Backfill existing seeds as approved
update marketing_use_cases
set approved = true,
    source = coalesce(nullif(source, ''), 'seed')
where approved = false;

create index if not exists idx_use_cases_approved_weight on marketing_use_cases(approved, weight desc);

-- ---- platform posts mapping (run -> platform post) ----
create table if not exists marketing_platform_posts (
  platform_post_id uuid primary key default gen_random_uuid(),
  run_id uuid not null references marketing_runs(run_id) on delete cascade,

  platform text not null,          -- instagram
  media_id text null,              -- IG media id
  permalink text null,
  status text not null default 'published',  -- published|failed|deleted

  published_at timestamptz null,
  payload_json jsonb not null default '{}'::jsonb,

  created_at timestamptz not null default now()
);

create index if not exists idx_platform_posts_run on marketing_platform_posts(run_id, created_at desc);
create index if not exists idx_platform_posts_media on marketing_platform_posts(platform, media_id);

-- ---- metrics snapshots (daily) ----
create table if not exists marketing_post_metrics (
  metrics_id uuid primary key default gen_random_uuid(),
  platform_post_id uuid not null references marketing_platform_posts(platform_post_id) on delete cascade,

  metric_date date not null,       -- daily snapshot
  impressions int null,
  reach int null,
  plays int null,
  likes int null,
  comments int null,
  shares int null,
  saves int null,
  profile_visits int null,
  follows int null,
  watch_time_ms bigint null,

  raw_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),

  unique(platform_post_id, metric_date)
);

create index if not exists idx_post_metrics_date on marketing_post_metrics(metric_date desc);