-- desifaces.ai INR Store Price Alignment Seed
-- Purpose:
--   Align India INR Business Yearly launch price to ₹83,900 across backend
--   canonical pricing and Apple/Google IAP mapping metadata.
--
-- Scope:
--   QA / launch configuration seed only.
--   This does NOT hard-code India behavior in app/backend logic.
--   It only updates country_code='IN', currency='INR' pricing configuration rows.
--
-- Canonical India launch prices after this seed:
--   Pro Monthly        ₹2,999
--   Pro Yearly         ₹29,900
--   Business Monthly   ₹9,900
--   Business Yearly    ₹83,900
--
-- Usage:
--   docker exec -i desifaces-db psql -U desifaces_admin -d desifaces < migrations/2026_07_03_desifaces_inr_store_price_sync.sql

\pset pager off

BEGIN;

\echo '--- 1. BACKUP SNAPSHOT: CURRENT INR SUBSCRIPTION PRICES ---'
select
  plan_code,
  interval_code,
  currency,
  country_code,
  price_money,
  metadata_json->>'plan_name' as plan_name,
  metadata_json->>'store_product_id' as store_product_id,
  updated_at
from public.pricing_plan_prices
where currency = 'INR'
  and country_code = 'IN'
  and plan_code in (
    'pro_monthly_v1',
    'pro_yearly_v1',
    'business_monthly_v1',
    'business_yearly_v1'
  )
order by plan_code;

\echo '--- 2. UPDATE CANONICAL BUSINESS YEARLY INR PRICE TO ₹83,900 ---'

update public.pricing_plan_prices
set
  price_money = 83900.00000000,
  metadata_json =
    coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
      'plan_name', 'Business Yearly',
      'store_product_id', 'ai.desifaces.business.yearly',
      'included_credits_total', 24000,
      'grant_credits', 24000,
      'previous_price_money', 99900,
      'price_adjustment_reason', 'Aligned India Business Yearly launch price across Apple, Google, and backend',
      'launch_price_sync', 'apple_google_backend_20260703',
      'india_launch_price_final', true
    ),
  updated_at = now()
where plan_code = 'business_yearly_v1'
  and currency = 'INR'
  and country_code = 'IN'
  and is_active = true;

\echo '--- 3. SYNC GOOGLE PLAY INR SUBSCRIPTION MAPPINGS FROM pricing_plan_prices ---'

with expected as (
  select *
  from (values
    ('ai.desifaces.pro.monthly', 'monthly', 'pro_monthly_v1', 'pro', 'Pro Monthly', 'month'),
    ('ai.desifaces.pro.yearly', 'yearly', 'pro_yearly_v1', 'pro', 'Pro Yearly', 'year'),
    ('ai.desifaces.business.monthly', 'monthly', 'business_monthly_v1', 'business', 'Business Monthly', 'month'),
    ('ai.desifaces.business.yearly', 'yearly', 'business_yearly_v1', 'business', 'Business Yearly', 'year')
  ) as x(google_product_id, base_plan_id, internal_plan_code, tier_code, display_name, billing_interval)
),
src as (
  select
    e.*,
    p.price_money
  from expected e
  join public.pricing_plan_prices p
    on p.plan_code = e.internal_plan_code
   and p.currency = 'INR'
   and p.country_code = 'IN'
   and p.is_active = true
)
update public.google_play_iap_product_mappings g
set
  metadata_json =
    coalesce(g.metadata_json, '{}'::jsonb)
    || jsonb_build_object(
      'tier_code', src.tier_code,
      'price_money', src.price_money,
      'display_name', src.display_name,
      'billing_interval', src.billing_interval,
      'synced_from', 'pricing_plan_prices',
      'launch_price_sync', 'apple_google_backend_20260703'
    ),
  is_active = true,
  updated_at = now()
from src
where g.google_product_id = src.google_product_id
  and g.base_plan_id = src.base_plan_id
  and g.internal_plan_code = src.internal_plan_code
  and g.currency = 'INR'
  and g.country_code = 'IN';

\echo '--- 4. INSERT MISSING GOOGLE PLAY INR SUBSCRIPTION MAPPINGS ---'

with expected as (
  select *
  from (values
    ('ai.desifaces.pro.monthly', 'monthly', 'pro_monthly_v1', 'pro', 'Pro Monthly', 'month'),
    ('ai.desifaces.pro.yearly', 'yearly', 'pro_yearly_v1', 'pro', 'Pro Yearly', 'year'),
    ('ai.desifaces.business.monthly', 'monthly', 'business_monthly_v1', 'business', 'Business Monthly', 'month'),
    ('ai.desifaces.business.yearly', 'yearly', 'business_yearly_v1', 'business', 'Business Yearly', 'year')
  ) as x(google_product_id, base_plan_id, internal_plan_code, tier_code, display_name, billing_interval)
),
src as (
  select
    e.*,
    p.price_money
  from expected e
  join public.pricing_plan_prices p
    on p.plan_code = e.internal_plan_code
   and p.currency = 'INR'
   and p.country_code = 'IN'
   and p.is_active = true
)
insert into public.google_play_iap_product_mappings (
  google_product_id,
  base_plan_id,
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
select
  src.google_product_id,
  src.base_plan_id,
  'subscription',
  null,
  'INR',
  'IN',
  null,
  src.internal_plan_code,
  true,
  jsonb_build_object(
    'tier_code', src.tier_code,
    'price_money', src.price_money,
    'display_name', src.display_name,
    'billing_interval', src.billing_interval,
    'synced_from', 'pricing_plan_prices',
    'launch_price_sync', 'apple_google_backend_20260703'
  ),
  now(),
  now()
from src
where not exists (
  select 1
  from public.google_play_iap_product_mappings g
  where g.google_product_id = src.google_product_id
    and g.base_plan_id = src.base_plan_id
    and g.internal_plan_code = src.internal_plan_code
    and g.currency = 'INR'
    and g.country_code = 'IN'
);

\echo '--- 5. SYNC APPLE INR SUBSCRIPTION MAPPING METADATA FROM pricing_plan_prices ---'

with expected as (
  select *
  from (values
    ('ai.desifaces.pro.monthly', 'pro_monthly_v1', 'pro', 'Pro Monthly', 'month'),
    ('ai.desifaces.pro.yearly', 'pro_yearly_v1', 'pro', 'Pro Yearly', 'year'),
    ('ai.desifaces.business.monthly', 'business_monthly_v1', 'business', 'Business Monthly', 'month'),
    ('ai.desifaces.business.yearly', 'business_yearly_v1', 'business', 'Business Yearly', 'year')
  ) as x(apple_product_id, internal_plan_code, tier_code, display_name, billing_interval)
),
src as (
  select
    e.*,
    p.price_money
  from expected e
  join public.pricing_plan_prices p
    on p.plan_code = e.internal_plan_code
   and p.currency = 'INR'
   and p.country_code = 'IN'
   and p.is_active = true
)
update public.apple_iap_product_mappings a
set
  metadata_json =
    coalesce(a.metadata_json, '{}'::jsonb)
    || jsonb_build_object(
      'tier_code', src.tier_code,
      'price_money', src.price_money,
      'display_name', src.display_name,
      'billing_interval', src.billing_interval,
      'synced_from', 'pricing_plan_prices',
      'launch_price_sync', 'apple_google_backend_20260703'
    ),
  is_active = true,
  updated_at = now()
from src
where a.apple_product_id = src.apple_product_id
  and a.internal_plan_code = src.internal_plan_code
  and a.currency = 'INR'
  and a.country_code = 'IN';

\echo '--- 6. INSERT MISSING APPLE INR SUBSCRIPTION MAPPINGS, IF ANY ---'

with expected as (
  select *
  from (values
    ('ai.desifaces.pro.monthly', 'pro_monthly_v1', 'pro', 'Pro Monthly', 'month'),
    ('ai.desifaces.pro.yearly', 'pro_yearly_v1', 'pro', 'Pro Yearly', 'year'),
    ('ai.desifaces.business.monthly', 'business_monthly_v1', 'business', 'Business Monthly', 'month'),
    ('ai.desifaces.business.yearly', 'business_yearly_v1', 'business', 'Business Yearly', 'year')
  ) as x(apple_product_id, internal_plan_code, tier_code, display_name, billing_interval)
),
src as (
  select
    e.*,
    p.price_money
  from expected e
  join public.pricing_plan_prices p
    on p.plan_code = e.internal_plan_code
   and p.currency = 'INR'
   and p.country_code = 'IN'
   and p.is_active = true
)
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
select
  src.apple_product_id,
  'subscription',
  null,
  'INR',
  'IN',
  null,
  src.internal_plan_code,
  true,
  jsonb_build_object(
    'tier_code', src.tier_code,
    'price_money', src.price_money,
    'display_name', src.display_name,
    'billing_interval', src.billing_interval,
    'synced_from', 'pricing_plan_prices',
    'launch_price_sync', 'apple_google_backend_20260703'
  ),
  now(),
  now()
from src
where not exists (
  select 1
  from public.apple_iap_product_mappings a
  where a.apple_product_id = src.apple_product_id
    and a.internal_plan_code = src.internal_plan_code
    and a.currency = 'INR'
    and a.country_code = 'IN'
);

\echo '--- 7. VERIFY CANONICAL INR PLAN PRICES ---'

select
  plan_code,
  interval_code,
  currency,
  country_code,
  price_money,
  metadata_json->>'plan_name' as plan_name,
  metadata_json->>'store_product_id' as store_product_id,
  metadata_json->>'included_credits_total' as included_credits_total,
  metadata_json->>'grant_credits' as grant_credits
from public.pricing_plan_prices
where currency = 'INR'
  and country_code = 'IN'
  and plan_code in (
    'pro_monthly_v1',
    'pro_yearly_v1',
    'business_monthly_v1',
    'business_yearly_v1'
  )
order by plan_code;

\echo '--- 8. VERIFY APPLE + GOOGLE INR SUBSCRIPTION ALIGNMENT ---'

with expected as (
  select *
  from (values
    ('ai.desifaces.pro.monthly', 'monthly', 'pro_monthly_v1'),
    ('ai.desifaces.pro.yearly', 'yearly', 'pro_yearly_v1'),
    ('ai.desifaces.business.monthly', 'monthly', 'business_monthly_v1'),
    ('ai.desifaces.business.yearly', 'yearly', 'business_yearly_v1')
  ) as x(product_id, base_plan_id, plan_code)
)
select
  e.product_id,
  e.base_plan_id,
  e.plan_code,
  p.price_money as canonical_price_money,
  g.metadata_json->>'price_money' as google_mapping_price_money,
  a.metadata_json->>'price_money' as apple_mapping_price_money,
  case
    when g.google_product_id is null then 'GOOGLE_MISSING'
    when a.apple_product_id is null then 'APPLE_MISSING'
    when (g.metadata_json->>'price_money')::numeric <> p.price_money then 'GOOGLE_MISMATCH'
    when (a.metadata_json->>'price_money')::numeric <> p.price_money then 'APPLE_MISMATCH'
    else 'OK'
  end as status
from expected e
join public.pricing_plan_prices p
  on p.plan_code = e.plan_code
 and p.currency = 'INR'
 and p.country_code = 'IN'
 and p.is_active = true
left join public.google_play_iap_product_mappings g
  on g.google_product_id = e.product_id
 and g.base_plan_id = e.base_plan_id
 and g.internal_plan_code = e.plan_code
 and g.currency = 'INR'
 and g.country_code = 'IN'
 and g.is_active = true
left join public.apple_iap_product_mappings a
  on a.apple_product_id = e.product_id
 and a.internal_plan_code = e.plan_code
 and a.currency = 'INR'
 and a.country_code = 'IN'
 and a.is_active = true
order by e.plan_code;

COMMIT;

\echo '--- COMMIT COMPLETE: INR APPLE/GOOGLE/BACKEND SUBSCRIPTION CONFIGURATION ALIGNED TO ₹83,900 BUSINESS YEARLY ---'
