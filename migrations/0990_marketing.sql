-- svc-marketing schema

create table if not exists marketing_runs (
  run_id uuid primary key,
  status text not null,
  stage text not null,
  mode text not null,
  recipe text not null,

  run_as_user_id uuid not null,
  bearer_token text null,

  cost_bucket text not null,
  cost_category text not null,

  input_json jsonb not null default '{}'::jsonb,
  planning_json jsonb not null default '{}'::jsonb,
  output_json jsonb not null default '{}'::jsonb,

  error_code text null,
  error_message text null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz null,
  finished_at timestamptz null
);

create index if not exists idx_marketing_runs_status_created on marketing_runs(status, created_at asc);
create index if not exists idx_marketing_runs_bucket_created on marketing_runs(cost_bucket, created_at desc);

create table if not exists marketing_assets (
  asset_id uuid primary key,
  run_id uuid not null references marketing_runs(run_id) on delete cascade,
  kind text not null,
  url text not null,
  content_type text not null,
  width int null,
  height int null,
  duration_sec double precision null,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_marketing_assets_run on marketing_assets(run_id, created_at asc);

create table if not exists marketing_use_cases (
  use_case_id uuid primary key,
  enabled boolean not null default true,
  weight double precision not null default 1.0,

  persona text not null,
  industry text not null,
  recipe text not null,
  campaign_type text not null default 'evergreen',
  season_event text null,

  tags text[] not null default '{}'::text[],

  product_anchor text null,
  default_offer text null,

  default_seconds int not null default 10,
  default_hook text null,
  base_overlay_lines jsonb not null default '[]'::jsonb,
  base_script text null,
  default_music_prompt text null,

  required_assets_json jsonb not null default '{}'::jsonb,
  notes text null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_use_cases_enabled_weight on marketing_use_cases(enabled, weight desc);
create index if not exists idx_use_cases_tags on marketing_use_cases using gin(tags);
create index if not exists idx_use_cases_persona_industry on marketing_use_cases(persona, industry);

create table if not exists marketing_schedules (
  schedule_id uuid primary key,
  name text not null,
  enabled boolean not null default true,

  freq text not null,
  hour int not null,
  minute int not null,
  dow text null,

  mode text not null,
  recipe text not null,
  persona text null,
  industry text null,
  tags text[] not null default '{}'::text[],
  season_event text null,
  offer text null,
  language_hint text not null default 'en',

  inputs_json jsonb not null default '{}'::jsonb,
  target_seconds int null,

  last_run_at timestamptz null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_marketing_schedules_enabled on marketing_schedules(enabled, freq);

create table if not exists ops_cost_ledger (
  id uuid primary key,
  created_at timestamptz not null default now(),

  run_id uuid not null,
  run_as_user_id uuid not null,

  cost_bucket text not null,
  cost_category text not null,
  cost_owner text not null,

  studio_type text not null,
  provider text not null,

  units double precision null,
  unit text null,

  cost_usd numeric not null default 0,
  credits numeric not null default 0,

  job_id uuid null,
  artifact_id uuid null,
  metadata_json jsonb not null default '{}'::jsonb
);

create index if not exists idx_ops_cost_ledger_run on ops_cost_ledger(run_id, created_at desc);
create index if not exists idx_ops_cost_ledger_bucket on ops_cost_ledger(cost_bucket, created_at desc);

alter table marketing_runs
  add column if not exists locked_by text,
  add column if not exists heartbeat_at timestamptz,
  add column if not exists lease_expires_at timestamptz;

create index if not exists idx_marketing_runs_status_created
  on marketing_runs(status, created_at);

create index if not exists idx_marketing_runs_lease
  on marketing_runs(status, lease_expires_at);