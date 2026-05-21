-- desifaces_pricing_plan_prices_schema_seed.sql
-- Purpose:
--   Create a single DB-driven source of truth for recurring subscription plan catalog
--   and Stripe checkout mapping, with support for monthly/yearly cleanly.
--
-- This table is intended to replace plan-catalog truth from config/env variables.
--
-- Existing sources that remain valid:
--   - pricing_credit_packs            -> top-up catalog
--   - billing_entitlements            -> current effective entitlement
--   - pricing_credit_accounts         -> live available/reserved balance
--   - payment_plan_subscriptions      -> subscription lifecycle / Stripe state
--
-- Notes:
--   1) This script backfills MONTHLY rows from existing pricing_tier_prices.
--   2) It seeds Enterprise as CONTACT SALES ONLY for both monthly and yearly.
--   3) It does NOT invent Pro/Business yearly prices. Templates are provided at the end.
--   4) If you already added stripe_price_id into pricing_tier_prices.metadata_json,
--      this script will carry it over for monthly rows.
--
-- Safe to run multiple times.

begin;

create table if not exists public.pricing_plan_prices (
    id              uuid                     not null default gen_random_uuid(),
    plan_code       text                     not null,
    tier_code       text                     not null,
    interval_code   text                     not null,
    currency        text                     not null,
    country_code    text                     not null default ''::text,
    price_money     numeric(18,8)            not null,
    stripe_price_id text,
    is_active       boolean                  not null default true,
    is_public       boolean                  not null default true,
    self_serve      boolean                  not null default true,
    contact_sales   boolean                  not null default false,
    display_order   integer                  not null default 0,
    metadata_json   jsonb                    not null default '{}'::jsonb,
    created_at      timestamp with time zone not null default now(),
    updated_at      timestamp with time zone not null default now(),
    constraint pricing_plan_prices_pkey primary key (id),
    constraint pricing_plan_prices_unique unique (plan_code, interval_code, currency, country_code),
    constraint pricing_plan_prices_tier_code_fkey foreign key (tier_code) references public.pricing_tiers(code),
    constraint pricing_plan_prices_interval_code_ck check (
        interval_code = any (array['monthly'::text, 'yearly'::text, 'custom'::text])
    )
);

create index if not exists ix_pricing_plan_prices_lookup
    on public.pricing_plan_prices (is_active, is_public, currency, country_code, display_order);

create index if not exists ix_pricing_plan_prices_tier_interval
    on public.pricing_plan_prices (tier_code, interval_code, currency, country_code);

create index if not exists ix_pricing_plan_prices_stripe_price_id
    on public.pricing_plan_prices (stripe_price_id);

do $$
begin
    if exists (
        select 1
        from pg_proc
        where proname = 'set_updated_at'
    ) then
        if not exists (
            select 1
            from pg_trigger
            where tgname = 'trg_pricing_plan_prices_updated_at'
        ) then
            create trigger trg_pricing_plan_prices_updated_at
            before update on public.pricing_plan_prices
            for each row execute function set_updated_at();
        end if;
    end if;
end $$;

-- Backfill monthly catalog rows from existing pricing_tier_prices.
-- This preserves any existing metadata, including stripe_price_id if already present there.

insert into public.pricing_plan_prices (
    plan_code,
    tier_code,
    interval_code,
    currency,
    country_code,
    price_money,
    stripe_price_id,
    is_active,
    is_public,
    self_serve,
    contact_sales,
    display_order,
    metadata_json
)
select
    case
        when p.tier_code = 'free' then 'free'
        when p.tier_code = 'creator' then 'creator'
        when p.tier_code = 'pro' then 'pro_monthly_v1'
        when p.tier_code = 'business' then 'business_monthly_v1'
        when p.tier_code = 'enterprise' then 'enterprise_contract_v1'
        when p.tier_code = 'developer' then 'developer'
        else p.tier_code
    end as plan_code,
    p.tier_code,
    'monthly' as interval_code,
    p.currency,
    coalesce(p.country_code, '') as country_code,
    p.monthly_price as price_money,
    nullif(trim(coalesce(p.metadata_json->>'stripe_price_id', '')), '') as stripe_price_id,
    p.is_active,
    case
        when p.metadata_json ? 'public' then coalesce((p.metadata_json->>'public')::boolean, true)
        when p.tier_code in ('free', 'pro', 'business') then true
        when p.tier_code = 'enterprise' then true
        else false
    end as is_public,
    case
        when p.metadata_json ? 'self_serve' then coalesce((p.metadata_json->>'self_serve')::boolean, false)
        when p.tier_code in ('pro', 'business') then true
        else false
    end as self_serve,
    case
        when p.metadata_json ? 'contact_sales' then coalesce((p.metadata_json->>'contact_sales')::boolean, false)
        when p.tier_code = 'enterprise' then true
        else false
    end as contact_sales,
    case
        when p.metadata_json ? 'display_order' then coalesce((p.metadata_json->>'display_order')::integer, 0)
        when p.tier_code = 'free' then 10
        when p.tier_code = 'creator' then 15
        when p.tier_code = 'pro' then 20
        when p.tier_code = 'business' then 30
        when p.tier_code = 'enterprise' then 40
        when p.tier_code = 'developer' then 50
        else 100
    end as display_order,
    coalesce(p.metadata_json, '{}'::jsonb)
      || jsonb_build_object(
            'migrated_from', 'pricing_tier_prices',
            'source_tier_code', p.tier_code,
            'source_currency', p.currency,
            'source_country_code', coalesce(p.country_code, ''),
            'plan_code',
                case
                    when p.tier_code = 'free' then 'free'
                    when p.tier_code = 'creator' then 'creator'
                    when p.tier_code = 'pro' then 'pro_monthly_v1'
                    when p.tier_code = 'business' then 'business_monthly_v1'
                    when p.tier_code = 'enterprise' then 'enterprise_contract_v1'
                    when p.tier_code = 'developer' then 'developer'
                    else p.tier_code
                end,
            'billing_family', p.tier_code,
            'interval_code', 'monthly'
        ) as metadata_json
from public.pricing_tier_prices p
where p.is_active = true
on conflict (plan_code, interval_code, currency, country_code)
do update set
    tier_code       = excluded.tier_code,
    price_money     = excluded.price_money,
    stripe_price_id = coalesce(excluded.stripe_price_id, public.pricing_plan_prices.stripe_price_id),
    is_active       = excluded.is_active,
    is_public       = excluded.is_public,
    self_serve      = excluded.self_serve,
    contact_sales   = excluded.contact_sales,
    display_order   = excluded.display_order,
    metadata_json   = excluded.metadata_json,
    updated_at      = now();

-- Force launch policy for creator/developer:
-- hidden from customer-facing billing UI unless you explicitly enable them later.
update public.pricing_plan_prices
set
    is_public = false,
    self_serve = false,
    contact_sales = false,
    metadata_json = coalesce(metadata_json, '{}'::jsonb)
        || jsonb_build_object('public', false, 'self_serve', false, 'contact_sales', false)
where tier_code in ('creator', 'developer');

-- Ensure enterprise exists as contact-sales only for BOTH monthly and yearly.
insert into public.pricing_plan_prices (
    plan_code,
    tier_code,
    interval_code,
    currency,
    country_code,
    price_money,
    stripe_price_id,
    is_active,
    is_public,
    self_serve,
    contact_sales,
    display_order,
    metadata_json
)
select
    'enterprise_contract_v1',
    'enterprise',
    'monthly',
    'USD',
    '',
    0.00000000,
    null,
    true,
    true,
    false,
    true,
    40,
    jsonb_build_object(
        'billing_family', 'enterprise',
        'interval_code', 'monthly',
        'contact_sales', true,
        'public', true
    )
where not exists (
    select 1
    from public.pricing_plan_prices
    where plan_code = 'enterprise_contract_v1'
      and interval_code = 'monthly'
      and currency = 'USD'
      and country_code = ''
);

insert into public.pricing_plan_prices (
    plan_code,
    tier_code,
    interval_code,
    currency,
    country_code,
    price_money,
    stripe_price_id,
    is_active,
    is_public,
    self_serve,
    contact_sales,
    display_order,
    metadata_json
)
select
    'enterprise_contract_v1',
    'enterprise',
    'monthly',
    'INR',
    'IN',
    0.00000000,
    null,
    true,
    true,
    false,
    true,
    40,
    jsonb_build_object(
        'billing_family', 'enterprise',
        'interval_code', 'monthly',
        'contact_sales', true,
        'public', true
    )
where not exists (
    select 1
    from public.pricing_plan_prices
    where plan_code = 'enterprise_contract_v1'
      and interval_code = 'monthly'
      and currency = 'INR'
      and country_code = 'IN'
);

insert into public.pricing_plan_prices (
    plan_code,
    tier_code,
    interval_code,
    currency,
    country_code,
    price_money,
    stripe_price_id,
    is_active,
    is_public,
    self_serve,
    contact_sales,
    display_order,
    metadata_json
)
select
    'enterprise_contract_v1',
    'enterprise',
    'yearly',
    'USD',
    '',
    0.00000000,
    null,
    true,
    true,
    false,
    true,
    41,
    jsonb_build_object(
        'billing_family', 'enterprise',
        'interval_code', 'yearly',
        'contact_sales', true,
        'public', true
    )
where not exists (
    select 1
    from public.pricing_plan_prices
    where plan_code = 'enterprise_contract_v1'
      and interval_code = 'yearly'
      and currency = 'USD'
      and country_code = ''
);

insert into public.pricing_plan_prices (
    plan_code,
    tier_code,
    interval_code,
    currency,
    country_code,
    price_money,
    stripe_price_id,
    is_active,
    is_public,
    self_serve,
    contact_sales,
    display_order,
    metadata_json
)
select
    'enterprise_contract_v1',
    'enterprise',
    'yearly',
    'INR',
    'IN',
    0.00000000,
    null,
    true,
    true,
    false,
    true,
    41,
    jsonb_build_object(
        'billing_family', 'enterprise',
        'interval_code', 'yearly',
        'contact_sales', true,
        'public', true
    )
where not exists (
    select 1
    from public.pricing_plan_prices
    where plan_code = 'enterprise_contract_v1'
      and interval_code = 'yearly'
      and currency = 'INR'
      and country_code = 'IN'
);

commit;

-- ============================================================
-- OPTIONAL: seed YEARLY rows for Pro / Business once business
-- pricing is approved. Replace placeholders before running.
-- ============================================================

-- Example template:
--
-- insert into public.pricing_plan_prices (
--     plan_code, tier_code, interval_code, currency, country_code,
--     price_money, stripe_price_id, is_active, is_public, self_serve,
--     contact_sales, display_order, metadata_json
-- ) values
-- (
--     'pro_yearly_v1',
--     'pro',
--     'yearly',
--     'USD',
--     '',
--     290.00000000,
--     'price_PRO_USD_YEARLY_REPLACE_ME',
--     true,
--     true,
--     true,
--     false,
--     21,
--     jsonb_build_object(
--         'billing_family', 'pro',
--         'interval_code', 'yearly',
--         'public', true,
--         'self_serve', true,
--         'contact_sales', false
--     )
-- ),
-- (
--     'pro_yearly_v1',
--     'pro',
--     'yearly',
--     'INR',
--     'IN',
--     24990.00000000,
--     'price_PRO_INR_YEARLY_REPLACE_ME',
--     true,
--     true,
--     true,
--     false,
--     21,
--     jsonb_build_object(
--         'billing_family', 'pro',
--         'interval_code', 'yearly',
--         'public', true,
--         'self_serve', true,
--         'contact_sales', false
--     )
-- );
--
-- Repeat similarly for business_yearly_v1.

-- Verification queries
-- select plan_code, tier_code, interval_code, currency, country_code,
--        price_money, stripe_price_id, is_active, is_public, self_serve,
--        contact_sales, display_order, metadata_json
-- from public.pricing_plan_prices
-- order by display_order, tier_code, interval_code, currency, country_code;

-- select *
-- from public.pricing_plan_prices
-- where is_active = true and is_public = true
-- order by display_order, currency, country_code;
