-- services/svc-commerce/db/migrations/20260306_001_business_vton_production.sql
-- Consolidated production schema for external business API, tenant auth,
-- platform-model catalog, strict garment routing, rate limits, auditability,
-- business jobs, outputs, and webhook delivery.
--
-- Notes:
-- 1) This migration is additive and does not modify the existing saree pipeline.
-- 2) It is designed to coexist with the current internal quote/confirm/status flow.
-- 3) External API traffic should write to commerce_business_jobs and can mirror/link
--    to existing internal job rows through request_json/resolved_json.

begin;

create extension if not exists pgcrypto;

-- -----------------------------------------------------------------------------
-- helper: touch updated_at automatically
-- -----------------------------------------------------------------------------
create or replace function public.df_set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- -----------------------------------------------------------------------------
-- tenants / external API credentials / webhooks
-- -----------------------------------------------------------------------------
create table if not exists public.commerce_tenants (
  id uuid primary key default gen_random_uuid(),
  code text not null unique,
  name text not null,
  environment text not null default 'production'
    check (environment in ('sandbox', 'production')),
  is_active boolean not null default true,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create trigger trg_commerce_tenants_updated_at
before update on public.commerce_tenants
for each row execute function public.df_set_updated_at();

create table if not exists public.commerce_api_credentials (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.commerce_tenants(id) on delete cascade,
  credential_type text not null
    check (credential_type in ('bearer_token', 'api_key', 'oauth_client')),
  key_id text not null unique,
  secret_hash text not null,
  scopes_json jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  expires_at timestamptz,
  last_used_at timestamptz,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_commerce_api_credentials_tenant_id
  on public.commerce_api_credentials(tenant_id);
create index if not exists idx_commerce_api_credentials_active
  on public.commerce_api_credentials(is_active);
create trigger trg_commerce_api_credentials_updated_at
before update on public.commerce_api_credentials
for each row execute function public.df_set_updated_at();

create table if not exists public.commerce_webhooks (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.commerce_tenants(id) on delete cascade,
  target_url text not null,
  signing_secret_hash text not null,
  event_types_json jsonb not null default '[]'::jsonb,
  is_active boolean not null default true,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_commerce_webhooks_tenant_id
  on public.commerce_webhooks(tenant_id);
create index if not exists idx_commerce_webhooks_active
  on public.commerce_webhooks(is_active);
create trigger trg_commerce_webhooks_updated_at
before update on public.commerce_webhooks
for each row execute function public.df_set_updated_at();

-- -----------------------------------------------------------------------------
-- tenant operational controls / quotas / rate limits
-- -----------------------------------------------------------------------------
create table if not exists public.commerce_tenant_rate_limits (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.commerce_tenants(id) on delete cascade,
  route_pattern text not null,
  requests_per_minute integer not null default 60 check (requests_per_minute > 0),
  requests_per_hour integer not null default 1000 check (requests_per_hour > 0),
  requests_per_day integer not null default 10000 check (requests_per_day > 0),
  max_concurrent_jobs integer not null default 25 check (max_concurrent_jobs > 0),
  max_payload_mb integer not null default 25 check (max_payload_mb > 0),
  is_active boolean not null default true,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, route_pattern)
);
create index if not exists idx_commerce_tenant_rate_limits_tenant_id
  on public.commerce_tenant_rate_limits(tenant_id);
create trigger trg_commerce_tenant_rate_limits_updated_at
before update on public.commerce_tenant_rate_limits
for each row execute function public.df_set_updated_at();

create table if not exists public.commerce_rate_limit_counters (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.commerce_tenants(id) on delete cascade,
  route_pattern text not null,
  bucket_kind text not null
    check (bucket_kind in ('minute', 'hour', 'day')),
  bucket_start timestamptz not null,
  request_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (tenant_id, route_pattern, bucket_kind, bucket_start)
);
create index if not exists idx_commerce_rate_limit_counters_lookup
  on public.commerce_rate_limit_counters(tenant_id, route_pattern, bucket_kind, bucket_start);
create trigger trg_commerce_rate_limit_counters_updated_at
before update on public.commerce_rate_limit_counters
for each row execute function public.df_set_updated_at();

-- -----------------------------------------------------------------------------
-- platform model catalog / assets / garment rules
-- -----------------------------------------------------------------------------
create table if not exists public.platform_models (
  id uuid primary key default gen_random_uuid(),
  model_code text not null unique,
  gender text not null
    check (gender in ('male', 'female', 'unisex')),
  age_band text,
  pose text,
  framing text,
  body_type text,
  region_tags jsonb not null default '[]'::jsonb,
  style_tags jsonb not null default '[]'::jsonb,
  quality_score double precision,
  face_quality_score double precision,
  body_visibility_score double precision,
  is_active boolean not null default true,
  source_container text,
  source_prefix text,
  meta_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (quality_score is null or (quality_score >= 0 and quality_score <= 1)),
  check (face_quality_score is null or (face_quality_score >= 0 and face_quality_score <= 1)),
  check (body_visibility_score is null or (body_visibility_score >= 0 and body_visibility_score <= 1))
);
create index if not exists idx_platform_models_gender on public.platform_models(gender);
create index if not exists idx_platform_models_active on public.platform_models(is_active);
create index if not exists idx_platform_models_framing on public.platform_models(framing);
create index if not exists idx_platform_models_pose on public.platform_models(pose);
create trigger trg_platform_models_updated_at
before update on public.platform_models
for each row execute function public.df_set_updated_at();

create table if not exists public.platform_model_assets (
  id uuid primary key default gen_random_uuid(),
  platform_model_id uuid not null references public.platform_models(id) on delete cascade,
  asset_role text not null
    check (asset_role in ('primary', 'alt_pose', 'alt_background', 'preview', 'mask')),
  asset_url text not null,
  width integer,
  height integer,
  content_type text,
  sort_order integer not null default 0,
  is_active boolean not null default true,
  qc_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_platform_model_assets_model_id
  on public.platform_model_assets(platform_model_id);
create index if not exists idx_platform_model_assets_active
  on public.platform_model_assets(is_active);
create trigger trg_platform_model_assets_updated_at
before update on public.platform_model_assets
for each row execute function public.df_set_updated_at();

create table if not exists public.garment_target_rules (
  garment_kind text primary key,
  allowed_genders jsonb not null default '[]'::jsonb,
  required_framing text,
  required_pose text,
  requires_full_body boolean not null default false,
  preferred_model_tags jsonb not null default '[]'::jsonb,
  strict_category_routing boolean not null default true,
  lower_body_visibility_required boolean not null default false,
  cultural_style_tags jsonb not null default '[]'::jsonb,
  meta_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create trigger trg_garment_target_rules_updated_at
before update on public.garment_target_rules
for each row execute function public.df_set_updated_at();

-- -----------------------------------------------------------------------------
-- external business jobs / outputs
-- -----------------------------------------------------------------------------
create table if not exists public.commerce_business_jobs (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.commerce_tenants(id) on delete cascade,
  client_job_id text,
  mode text not null
    check (mode in ('platform_model_tryon', 'customer_tryon', 'hybrid_tryon')),
  status text not null
    check (status in ('queued', 'processing', 'succeeded', 'failed', 'cancel_requested', 'cancelled')),
  stage text not null
    check (stage in (
      'received',
      'normalizing_inputs',
      'selecting_platform_model',
      'running_vton',
      'quality_check',
      'uploading_outputs',
      'succeeded',
      'failed',
      'cancel_requested',
      'cancelled'
    )),
  idempotency_key text,
  route_pattern text not null default '/api/commerce/v1/jobs',
  request_json jsonb not null default '{}'::jsonb,
  resolved_json jsonb not null default '{}'::jsonb,
  artifact_status_json jsonb not null default '{}'::jsonb,
  qc_json jsonb not null default '{}'::jsonb,
  error_json jsonb not null default '{}'::jsonb,
  webhook_url text,
  retry_count integer not null default 0,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists uq_commerce_business_jobs_tenant_client_job_id
  on public.commerce_business_jobs(tenant_id, client_job_id)
  where client_job_id is not null;
create unique index if not exists uq_commerce_business_jobs_tenant_idempotency_key
  on public.commerce_business_jobs(tenant_id, idempotency_key)
  where idempotency_key is not null;
create index if not exists idx_commerce_business_jobs_tenant_status
  on public.commerce_business_jobs(tenant_id, status, created_at desc);
create index if not exists idx_commerce_business_jobs_stage
  on public.commerce_business_jobs(stage, created_at desc);
create trigger trg_commerce_business_jobs_updated_at
before update on public.commerce_business_jobs
for each row execute function public.df_set_updated_at();

create table if not exists public.commerce_job_outputs (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.commerce_business_jobs(id) on delete cascade,
  output_url text not null,
  width integer,
  height integer,
  content_type text,
  rank integer not null default 1,
  score double precision,
  is_best boolean not null default false,
  qc_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (score is null or (score >= 0 and score <= 1))
);
create index if not exists idx_commerce_job_outputs_job_id
  on public.commerce_job_outputs(job_id);
create unique index if not exists uq_commerce_job_outputs_best_per_job
  on public.commerce_job_outputs(job_id)
  where is_best = true;

-- -----------------------------------------------------------------------------
-- webhook deliveries / request audit logs
-- -----------------------------------------------------------------------------
create table if not exists public.commerce_webhook_deliveries (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.commerce_tenants(id) on delete cascade,
  webhook_id uuid not null references public.commerce_webhooks(id) on delete cascade,
  job_id uuid references public.commerce_business_jobs(id) on delete cascade,
  event_type text not null,
  request_json jsonb not null default '{}'::jsonb,
  response_code integer,
  response_body text,
  delivery_status text not null
    check (delivery_status in ('queued', 'sent', 'failed', 'abandoned')),
  attempt_count integer not null default 0,
  next_attempt_at timestamptz,
  last_attempt_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_commerce_webhook_deliveries_tenant
  on public.commerce_webhook_deliveries(tenant_id, created_at desc);
create index if not exists idx_commerce_webhook_deliveries_status_next_attempt
  on public.commerce_webhook_deliveries(delivery_status, next_attempt_at);
create trigger trg_commerce_webhook_deliveries_updated_at
before update on public.commerce_webhook_deliveries
for each row execute function public.df_set_updated_at();

create table if not exists public.commerce_request_audit_logs (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid references public.commerce_tenants(id) on delete set null,
  credential_id uuid references public.commerce_api_credentials(id) on delete set null,
  request_id text not null,
  route_pattern text not null,
  method text not null,
  http_status integer,
  client_job_id text,
  business_job_id uuid references public.commerce_business_jobs(id) on delete set null,
  remote_addr text,
  user_agent text,
  payload_size_bytes bigint,
  duration_ms integer,
  provider_name text,
  provider_request_id text,
  audit_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists idx_commerce_request_audit_logs_tenant_created
  on public.commerce_request_audit_logs(tenant_id, created_at desc);
create index if not exists idx_commerce_request_audit_logs_request_id
  on public.commerce_request_audit_logs(request_id);

-- -----------------------------------------------------------------------------
-- useful views
-- -----------------------------------------------------------------------------
create or replace view public.commerce_active_concurrent_jobs_v as
select
  tenant_id,
  count(*)::integer as active_job_count
from public.commerce_business_jobs
where status in ('queued', 'processing', 'cancel_requested')
group by tenant_id;

-- -----------------------------------------------------------------------------
-- seed: strict garment routing rules for saree + non-saree Indian garments
-- -----------------------------------------------------------------------------
insert into public.garment_target_rules (
  garment_kind,
  allowed_genders,
  required_framing,
  required_pose,
  requires_full_body,
  preferred_model_tags,
  strict_category_routing,
  lower_body_visibility_required,
  cultural_style_tags,
  meta_json
) values
  (
    'saree_set',
    '["female"]'::jsonb,
    'full_body',
    'front',
    true,
    '["india", "ethnic", "catalog"]'::jsonb,
    true,
    true,
    '["indian", "saree"]'::jsonb,
    '{"pipeline":"frozen_saree_overlay"}'::jsonb
  ),
  (
    'salwar_suit',
    '["female"]'::jsonb,
    'full_body',
    'front',
    true,
    '["india", "ethnic", "catalog"]'::jsonb,
    true,
    true,
    '["indian", "salwar_suit"]'::jsonb,
    '{"qc_min_category_compliance":0.90}'::jsonb
  ),
  (
    'lehenga_set',
    '["female"]'::jsonb,
    'full_body',
    'front',
    true,
    '["india", "ethnic", "festive", "catalog"]'::jsonb,
    true,
    true,
    '["indian", "lehenga"]'::jsonb,
    '{"qc_min_category_compliance":0.92}'::jsonb
  ),
  (
    'kurti_leggings_set',
    '["female"]'::jsonb,
    'full_body',
    'front',
    true,
    '["india", "ethnic", "catalog"]'::jsonb,
    true,
    true,
    '["indian", "kurti"]'::jsonb,
    '{"qc_min_category_compliance":0.88}'::jsonb
  ),
  (
    'kurta_pyjama',
    '["male"]'::jsonb,
    'three_quarter_or_full_body',
    'front',
    false,
    '["india", "ethnic", "catalog"]'::jsonb,
    true,
    true,
    '["indian", "kurta_pyjama"]'::jsonb,
    '{"qc_min_category_compliance":0.90}'::jsonb
  ),
  (
    'dhoti_kurta',
    '["male"]'::jsonb,
    'full_body',
    'front',
    true,
    '["india", "ethnic", "traditional", "catalog"]'::jsonb,
    true,
    true,
    '["indian", "dhoti_kurta"]'::jsonb,
    '{"qc_min_category_compliance":0.94}'::jsonb
  ),
  (
    'sherwani',
    '["male"]'::jsonb,
    'three_quarter_or_full_body',
    'front',
    false,
    '["india", "ethnic", "wedding", "catalog"]'::jsonb,
    true,
    false,
    '["indian", "sherwani"]'::jsonb,
    '{"qc_min_category_compliance":0.91}'::jsonb
  ),
  (
    'nehru_jacket_set',
    '["male"]'::jsonb,
    'three_quarter_or_full_body',
    'front',
    false,
    '["india", "ethnic", "formal", "catalog"]'::jsonb,
    true,
    false,
    '["indian", "nehru_jacket"]'::jsonb,
    '{"qc_min_category_compliance":0.88}'::jsonb
  ),
  (
    'kurta_only',
    '["male", "female", "unisex"]'::jsonb,
    'three_quarter_or_full_body',
    'front',
    false,
    '["india", "ethnic", "catalog"]'::jsonb,
    true,
    false,
    '["indian", "kurta"]'::jsonb,
    '{"qc_min_category_compliance":0.86}'::jsonb
  )
on conflict (garment_kind) do update set
  allowed_genders = excluded.allowed_genders,
  required_framing = excluded.required_framing,
  required_pose = excluded.required_pose,
  requires_full_body = excluded.requires_full_body,
  preferred_model_tags = excluded.preferred_model_tags,
  strict_category_routing = excluded.strict_category_routing,
  lower_body_visibility_required = excluded.lower_body_visibility_required,
  cultural_style_tags = excluded.cultural_style_tags,
  meta_json = excluded.meta_json,
  updated_at = now();

commit;
