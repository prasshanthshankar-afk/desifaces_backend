-- desifaces_apple_iap_tables.sql
-- Purpose:
--   Add Apple IAP transaction/audit tables to svc-pricing.
--   This complements the already-seeded public.apple_iap_product_mappings table.
--
-- Notes:
--   - Safe to rerun where possible.
--   - Uses text status fields to avoid premature enum lock-in.
--   - Add this through your normal migration path if preferred.

begin;

create table if not exists public.apple_iap_transactions (
  id                     uuid primary key default gen_random_uuid(),
  user_id                uuid not null,
  apple_product_id       text not null,
  product_type           text not null check (product_type in ('subscription', 'consumable')),
  transaction_id         text not null,
  original_transaction_id text null,
  app_account_token      uuid null,
  environment            text not null,
  currency               text not null default '',
  country_code           text not null default '',
  storefront             text null,
  storefront_id          text null,
  purchase_date          timestamptz null,
  expires_date           timestamptz null,
  ownership_type         text null,
  transaction_reason     text null,
  raw_signed_transaction text not null,
  raw_signed_renewal     text null,
  raw_decoded_json       jsonb not null default '{}'::jsonb,
  processed_status       text not null default 'processed',
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

create unique index if not exists ux_apple_iap_transactions_transaction_id
  on public.apple_iap_transactions (transaction_id);

create index if not exists ix_apple_iap_transactions_original_transaction_id
  on public.apple_iap_transactions (original_transaction_id);

create index if not exists ix_apple_iap_transactions_user_id
  on public.apple_iap_transactions (user_id);

create index if not exists ix_apple_iap_transactions_product
  on public.apple_iap_transactions (apple_product_id, product_type);

create table if not exists public.apple_iap_notification_events (
  id                     uuid primary key default gen_random_uuid(),
  notification_uuid      text not null,
  notification_type      text not null,
  subtype                text null,
  environment            text not null,
  signed_payload         text not null,
  decoded_payload_json   jsonb not null default '{}'::jsonb,
  transaction_id         text null,
  original_transaction_id text null,
  app_account_token      uuid null,
  processing_status      text not null default 'processed',
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

create unique index if not exists ux_apple_iap_notification_events_notification_uuid
  on public.apple_iap_notification_events (notification_uuid);

create index if not exists ix_apple_iap_notification_events_original_transaction_id
  on public.apple_iap_notification_events (original_transaction_id);

create index if not exists ix_apple_iap_notification_events_transaction_id
  on public.apple_iap_notification_events (transaction_id);

create index if not exists ix_apple_iap_notification_events_app_account_token
  on public.apple_iap_notification_events (app_account_token);

commit;

-- Verification:
-- select count(*) from public.apple_iap_transactions;
-- select count(*) from public.apple_iap_notification_events;
