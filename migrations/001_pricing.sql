create table if not exists pricing_tiers (
  code text primary key,                         -- free, pro, enterprise, developer
  name text not null,
  monthly_grant_credits bigint not null default 0,
  is_active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists pricing_user_entitlements (
  user_id uuid primary key,
  tier_code text not null references pricing_tiers(code),
  effective_from timestamptz not null default now(),
  metadata_json jsonb not null default '{}'::jsonb
);

create table if not exists pricing_credit_accounts (
  user_id uuid primary key,
  balance_credits bigint not null default 0,
  updated_at timestamptz not null default now()
);

-- SKU catalog: product semantics and default credit price
create table if not exists pricing_skus (
  code text primary key,                         -- IMG_STD_GEN, VIDEO_SEC_STANDARD, ...
  name text not null,
  unit text not null,                            -- run, second, 1k_chars, 1k_calls, minute
  category text not null,                        -- face, audio, fusion, music, api
  provider_hint text null,                       -- fal, openai, azure_tts, heygen, native
  default_unit_credits bigint not null,          -- default credits per unit
  status text not null default 'active',         -- active, inactive
  effective_from timestamptz not null default now(),
  effective_to timestamptz null,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Credit value in money for a currency (money-per-credit)
-- Example: USD -> 0.01 per credit (if 100 credits = 1 USD)
create table if not exists pricing_credit_value (
  currency text not null,                        -- USD, INR
  money_per_credit numeric(18,8) not null,       -- e.g. 0.01000000 (USD)
  rounding_mode text not null default 'ceil',    -- ceil, round, floor
  effective_from timestamptz not null default now(),
  effective_to timestamptz null,
  primary key(currency, effective_from)
);

-- FX rates (optional; you can also inject rates from another svc)
create table if not exists pricing_fx_rates (
  base_currency text not null,                   -- USD
  quote_currency text not null,                  -- INR
  rate numeric(18,8) not null,                   -- 1 USD = rate INR
  as_of timestamptz not null default now(),
  primary key(base_currency, quote_currency, as_of)
);

-- Pricebooks define regional/channel/tier pricing context + multipliers
create table if not exists pricing_pricebooks (
  id uuid primary key,
  name text not null,
  country_code text null,                        -- 'IN', 'US', null means global
  currency text not null,                        -- USD, INR
  channel text not null,                         -- web, mobile, api
  tier_code text null references pricing_tiers(code), -- optional
  multiplier numeric(10,6) not null default 1.0,  -- regional/tier multiplier on credits
  is_active boolean not null default true,
  effective_from timestamptz not null default now(),
  effective_to timestamptz null,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Per-SKU overrides inside a pricebook:
-- you can override credits OR override money OR both
create table if not exists pricing_sku_prices (
  pricebook_id uuid not null references pricing_pricebooks(id) on delete cascade,
  sku_code text not null references pricing_skus(code),
  unit_credits_override bigint null,
  unit_money_override numeric(18,8) null,        -- if you want absolute money price for this currency
  min_qty bigint null,
  max_qty bigint null,
  metadata_json jsonb not null default '{}'::jsonb,
  primary key(pricebook_id, sku_code)
);

-- Ledger events (idempotent)
create table if not exists pricing_credit_ledger_events (
  id uuid primary key,
  user_id uuid not null,
  event_type text not null,                      -- grant, topup, consume, refund, adjust
  credits_delta bigint not null,                 -- + or -
  sku_code text null references pricing_skus(code),
  quantity bigint null,
  unit_credits bigint null,
  idempotency_key text not null,
  country_code text null,
  currency text null,
  money_amount numeric(18,8) null,               -- money charged (if applicable)
  channel text null,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique(user_id, idempotency_key)
);

create index if not exists ix_pricing_ledger_user_created
  on pricing_credit_ledger_events(user_id, created_at desc);

create index if not exists ix_pricing_pricebooks_lookup
  on pricing_pricebooks(is_active, currency, channel, country_code, tier_code, effective_from desc);