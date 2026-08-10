-- desifaces_apple_iap_subscription_mappings_seed.sql
-- Purpose:
--   Seed Apple IAP subscription product mappings into public.apple_iap_product_mappings
--   using DesiFaces launch plan codes.
--
-- Assumptions:
--   - Your existing apple_iap_product_mappings table already exists from the credit-pack seed.
--   - DesiFaces launch plan_code values are:
--       pro_monthly_v1
--       pro_yearly_v1
--       business_monthly_v1
--       business_yearly_v1
--
-- Important:
--   If your actual plan_code literals differ in billing_entitlements / payment_plan_subscriptions,
--   update only the 4 internal_plan_code values below before running.
--
-- Notes:
--   - Apple subscriptions are logical products; storefront pricing is configured in App Store Connect.
--   - We seed INR/IN and USD/global rows so the mapping remains provider/storefront-aware,
--     consistent with the credit-pack mapping design.
--   - Safe to rerun: uses ON CONFLICT upsert behavior.

begin;

create table if not exists public.apple_iap_product_mappings (
  apple_product_id   text        not null,
  product_type       text        not null check (product_type in ('subscription', 'consumable')),
  credits            bigint      null,
  currency           text        not null default '',
  country_code       text        not null default '',
  internal_pack_code text        null,
  internal_plan_code text        null,
  is_active          boolean     not null default true,
  metadata_json      jsonb       not null default '{}'::jsonb,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  primary key (apple_product_id, currency, country_code)
);

create index if not exists idx_apple_iap_product_mappings_plan_code
  on public.apple_iap_product_mappings (internal_plan_code);

insert into public.apple_iap_product_mappings (
  apple_product_id,
  product_type,
  credits,
  currency,
  country_code,
  internal_pack_code,
  internal_plan_code,
  is_active,
  metadata_json,
  created_at,
  updated_at
)
values
  (
    'ai.desifaces.pro.monthly',
    'subscription',
    null,
    'INR',
    'IN',
    null,
    'pro_monthly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'pro',
      'billing_interval', 'month',
      'display_name', 'Pro Monthly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  ),
  (
    'ai.desifaces.pro.monthly',
    'subscription',
    null,
    'USD',
    '',
    null,
    'pro_monthly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'pro',
      'billing_interval', 'month',
      'display_name', 'Pro Monthly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  ),
  (
    'ai.desifaces.pro.yearly',
    'subscription',
    null,
    'INR',
    'IN',
    null,
    'pro_yearly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'pro',
      'billing_interval', 'year',
      'display_name', 'Pro Yearly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  ),
  (
    'ai.desifaces.pro.yearly',
    'subscription',
    null,
    'USD',
    '',
    null,
    'pro_yearly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'pro',
      'billing_interval', 'year',
      'display_name', 'Pro Yearly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  ),
  (
    'ai.desifaces.business.monthly',
    'subscription',
    null,
    'INR',
    'IN',
    null,
    'business_monthly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'business',
      'billing_interval', 'month',
      'display_name', 'Business Monthly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  ),
  (
    'ai.desifaces.business.monthly',
    'subscription',
    null,
    'USD',
    '',
    null,
    'business_monthly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'business',
      'billing_interval', 'month',
      'display_name', 'Business Monthly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  ),
  (
    'ai.desifaces.business.yearly',
    'subscription',
    null,
    'INR',
    'IN',
    null,
    'business_yearly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'business',
      'billing_interval', 'year',
      'display_name', 'Business Yearly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  ),
  (
    'ai.desifaces.business.yearly',
    'subscription',
    null,
    'USD',
    '',
    null,
    'business_yearly_v1',
    true,
    jsonb_build_object(
      'tier_code', 'business',
      'billing_interval', 'year',
      'display_name', 'Business Yearly',
      'seed_source', 'apple_iap_subscription_seed'
    ),
    now(),
    now()
  )
on conflict (apple_product_id, currency, country_code)
do update set
  product_type       = excluded.product_type,
  credits            = excluded.credits,
  internal_pack_code = excluded.internal_pack_code,
  internal_plan_code = excluded.internal_plan_code,
  is_active          = excluded.is_active,
  metadata_json      = excluded.metadata_json,
  updated_at         = now();

commit;

-- Verification:
-- select
--   apple_product_id,
--   product_type,
--   currency,
--   country_code,
--   internal_plan_code,
--   metadata_json
-- from public.apple_iap_product_mappings
-- where product_type = 'subscription'
-- order by apple_product_id, currency, country_code;
