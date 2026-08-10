-- desifaces_apple_iap_credit_mappings_seed.sql
-- Purpose:
--   Create and seed Apple IAP credit-pack mappings from the current active
--   public.pricing_credit_packs catalog.
--
-- Notes:
--   - Uses 3 logical Apple consumables:
--       ai.desifaces.credits.1000
--       ai.desifaces.credits.5000
--       ai.desifaces.credits.15000
--   - Preserves your existing INR/USD internal pack codes for accounting/reporting.
--   - Safe to rerun: uses ON CONFLICT upsert behavior.
--
-- Expected current active packs:
--   PACK_INR_1000, PACK_INR_5000, PACK_INR_15000,
--   PACK_USD_1000, PACK_USD_5000, PACK_USD_15000

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

create index if not exists idx_apple_iap_product_mappings_pack_code
  on public.apple_iap_product_mappings (internal_pack_code);

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
select
  case
    when p.credits = 1000  then 'ai.desifaces.credits.1000'
    when p.credits = 5000  then 'ai.desifaces.credits.5000'
    when p.credits = 15000 then 'ai.desifaces.credits.15000'
    else null
  end as apple_product_id,
  'consumable' as product_type,
  p.credits,
  p.currency,
  coalesce(p.country_code, '') as country_code,
  p.code as internal_pack_code,
  null as internal_plan_code,
  true as is_active,
  (
    coalesce(p.metadata_json, '{}'::jsonb)
    || jsonb_build_object(
         'display_name', p.name,
         'seed_source', 'pricing_credit_packs',
         'seeded_from_pack_code', p.code
       )
  ) as metadata_json,
  now() as created_at,
  now() as updated_at
from public.pricing_credit_packs p
where p.is_active = true
  and p.code in (
    'PACK_INR_1000',
    'PACK_INR_5000',
    'PACK_INR_15000',
    'PACK_USD_1000',
    'PACK_USD_5000',
    'PACK_USD_15000'
  )
  and p.credits in (1000, 5000, 15000)
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
--   credits,
--   currency,
--   country_code,
--   internal_pack_code,
--   metadata_json
-- from public.apple_iap_product_mappings
-- order by apple_product_id, currency, country_code;
