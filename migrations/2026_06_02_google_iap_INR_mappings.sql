begin;

-- Google Play INR subscription mappings
with plan_map as (
  select *
  from (
    values
      ('ai.desifaces.pro.monthly',      'monthly', 'subscription', 'pro_monthly_v1'),
      ('ai.desifaces.pro.yearly',       'yearly',  'subscription', 'pro_yearly_v1'),
      ('ai.desifaces.business.monthly', 'monthly', 'subscription', 'business_monthly_v1'),
      ('ai.desifaces.business.yearly',  'yearly',  'subscription', 'business_yearly_v1')
  ) as v(google_product_id, base_plan_id, product_type, internal_plan_code)
),
plan_prices as (
  select
    p.plan_code,
    p.tier_code,
    p.interval_code,
    p.price_money
  from public.pricing_plan_prices p
  where p.currency = 'INR'
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
  m.google_product_id,
  m.base_plan_id,
  m.product_type,
  null::bigint,
  'INR',
  'IN',
  null::text,
  m.internal_plan_code,
  true,
  jsonb_build_object(
    'tier_code', pp.tier_code,
    'price_money', pp.price_money,
    'seed_source', 'google_play_iap_inr_seed_20260602',
    'display_name',
      case m.internal_plan_code
        when 'pro_monthly_v1' then 'Pro Monthly'
        when 'pro_yearly_v1' then 'Pro Yearly'
        when 'business_monthly_v1' then 'Business Monthly'
        when 'business_yearly_v1' then 'Business Yearly'
        else m.internal_plan_code
      end,
    'billing_interval',
      case pp.interval_code
        when 'yearly' then 'year'
        else 'month'
      end
  ),
  now(),
  now()
from plan_map m
join plan_prices pp
  on pp.plan_code = m.internal_plan_code
on conflict (google_product_id, base_plan_id, currency, country_code)
do update set
  product_type = excluded.product_type,
  credits = excluded.credits,
  internal_plan_code = excluded.internal_plan_code,
  internal_pack_code = excluded.internal_pack_code,
  is_active = true,
  metadata_json = coalesce(public.google_play_iap_product_mappings.metadata_json, '{}'::jsonb)
                  || excluded.metadata_json,
  updated_at = now();

-- Google Play INR consumable/top-up mappings
with pack_map as (
  select *
  from (
    values
      ('ai.desifaces.credits.1000',  '', 'consumable', 'PACK_INR_1000'),
      ('ai.desifaces.credits.5000',  '', 'consumable', 'PACK_INR_5000'),
      ('ai.desifaces.credits.15000', '', 'consumable', 'PACK_INR_15000')
  ) as v(google_product_id, base_plan_id, product_type, internal_pack_code)
),
packs as (
  select
    code,
    name,
    credits,
    price_money,
    metadata_json
  from public.pricing_credit_packs
  where currency = 'INR'
    and country_code = 'IN'
    and is_active = true
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
  m.google_product_id,
  m.base_plan_id,
  m.product_type,
  p.credits,
  'INR',
  'IN',
  m.internal_pack_code,
  null::text,
  true,
  jsonb_build_object(
    'price_money', p.price_money,
    'seed_source', 'google_play_iap_inr_seed_20260602',
    'display_name', p.name,
    'seeded_from_pack_code', p.code
  ) || coalesce(p.metadata_json, '{}'::jsonb),
  now(),
  now()
from pack_map m
join packs p
  on p.code = m.internal_pack_code
on conflict (google_product_id, base_plan_id, currency, country_code)
do update set
  product_type = excluded.product_type,
  credits = excluded.credits,
  internal_pack_code = excluded.internal_pack_code,
  internal_plan_code = excluded.internal_plan_code,
  is_active = true,
  metadata_json = coalesce(public.google_play_iap_product_mappings.metadata_json, '{}'::jsonb)
                  || excluded.metadata_json,
  updated_at = now();

-- Verify before commit
select
  google_product_id,
  base_plan_id,
  product_type,
  credits,
  currency,
  country_code,
  internal_pack_code,
  internal_plan_code,
  is_active,
  metadata_json
from public.google_play_iap_product_mappings
where currency = 'INR'
  and country_code = 'IN'
order by product_type, internal_plan_code, internal_pack_code, google_product_id, base_plan_id;

commit;

begin;

update public.pricing_plan_prices
set metadata_json = coalesce(metadata_json, '{}'::jsonb) || jsonb_build_object('plan_name', 'Pro Monthly'),
    updated_at = now()
where plan_code = 'pro_monthly_v1'
  and interval_code = 'monthly';

update public.pricing_plan_prices
set metadata_json = coalesce(metadata_json, '{}'::jsonb) || jsonb_build_object('plan_name', 'Pro Yearly'),
    updated_at = now()
where plan_code = 'pro_yearly_v1'
  and interval_code = 'yearly';

update public.pricing_plan_prices
set metadata_json = coalesce(metadata_json, '{}'::jsonb) || jsonb_build_object('plan_name', 'Business Monthly'),
    updated_at = now()
where plan_code = 'business_monthly_v1'
  and interval_code = 'monthly';

update public.pricing_plan_prices
set metadata_json = coalesce(metadata_json, '{}'::jsonb) || jsonb_build_object('plan_name', 'Business Yearly'),
    updated_at = now()
where plan_code = 'business_yearly_v1'
  and interval_code = 'yearly';

commit;