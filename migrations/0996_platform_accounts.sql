-- services/svc-marketing/app/app/db/migrations/006_platform_accounts.sql
-- Platform accounts for publishing (YouTube/Instagram/etc).
-- Secrets are stored as *refs* (env:VAR, keyvault:..., literal:...) and resolved by SecretProvider.

create extension if not exists pgcrypto;

create table if not exists marketing_platform_accounts (
  platform_account_id uuid primary key default gen_random_uuid(),

  platform text not null,                  -- youtube | instagram | ...
  account_name text not null,
  enabled boolean not null default true,

  -- YouTube-specific (still ok for other platforms to leave null)
  channel_id text null,
  oauth_client_id text null,

  oauth_client_secret_ref text null,       -- e.g. env:YT_CLIENT_SECRET
  oauth_refresh_token_ref text null,       -- e.g. env:YT_REFRESH_TOKEN

  -- cache access token to reduce refresh calls (optional)
  access_token_cache_json jsonb not null default '{}'::jsonb,

  scopes text[] not null default '{}'::text[],

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_platform_accounts_platform on marketing_platform_accounts(platform, enabled);