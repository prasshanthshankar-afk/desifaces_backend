begin;

create table if not exists public.google_play_iap_product_mappings (
  google_product_id text not null,
  base_plan_id text not null default '',
  product_type text not null,
  credits bigint,
  currency text not null default '',
  country_code text not null default '',
  internal_pack_code text,
  internal_plan_code text,
  is_active boolean not null default true,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (google_product_id, base_plan_id, currency, country_code),
  constraint google_play_iap_product_mappings_type_ck
    check (product_type in ('subscription', 'consumable'))
);

create index if not exists idx_google_play_iap_mappings_pack_code
  on public.google_play_iap_product_mappings (internal_pack_code);

create index if not exists idx_google_play_iap_mappings_plan_code
  on public.google_play_iap_product_mappings (internal_plan_code);

create table if not exists public.google_play_iap_purchases (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  google_product_id text not null,
  base_plan_id text not null default '',
  product_type text not null,
  package_name text not null,
  purchase_token_hash text not null,
  order_id text,
  linked_purchase_token_hash text,
  purchase_state text,
  acknowledgement_state text,
  consumption_state text,
  subscription_state text,
  internal_pack_code text,
  internal_plan_code text,
  raw_purchase_json jsonb not null default '{}'::jsonb,
  processed_status text not null default 'processed',
  fulfillment_state text not null default 'pending',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint google_play_iap_purchases_type_ck
    check (product_type in ('subscription', 'consumable')),
  constraint ux_google_play_iap_purchase_token
    unique (package_name, google_product_id, purchase_token_hash)
);

create index if not exists ix_google_play_iap_purchases_user_created
  on public.google_play_iap_purchases (user_id, created_at desc);

create index if not exists ix_google_play_iap_purchases_product
  on public.google_play_iap_purchases (google_product_id, product_type);

create table if not exists public.google_play_iap_notification_events (
  id uuid primary key default gen_random_uuid(),
  message_id text not null,
  notification_type text not null,
  package_name text,
  google_product_id text,
  purchase_token_hash text,
  decoded_payload_json jsonb not null default '{}'::jsonb,
  processing_status text not null default 'processed',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint ux_google_play_iap_notification_message
    unique (message_id)
);

-- Keep backend catalog in sync with locked Apple / Google / Stripe launch prices.
update public.pricing_credit_packs
set price_money = 9.99,
    metadata_json = coalesce(metadata_json, '{}'::jsonb) || '{"launch_price_sync":"apple_google_stripe_2026_05_23"}'::jsonb
where code = 'PACK_USD_1000';

update public.pricing_credit_packs
set price_money = 39.99,
    metadata_json = coalesce(metadata_json, '{}'::jsonb) || '{"launch_price_sync":"apple_google_stripe_2026_05_23"}'::jsonb
where code = 'PACK_USD_5000';

update public.pricing_credit_packs
set price_money = 99.99,
    metadata_json = coalesce(metadata_json, '{}'::jsonb) || '{"launch_price_sync":"apple_google_stripe_2026_05_23"}'::jsonb
where code = 'PACK_USD_15000';

-- Google Play subscription mappings. US/default USD first release.
insert into public.google_play_iap_product_mappings (
  google_product_id, base_plan_id, product_type, credits, currency, country_code,
  internal_pack_code, internal_plan_code, is_active, metadata_json
) values
  ('ai.desifaces.pro.monthly', 'monthly', 'subscription', null, 'USD', '', null, 'pro_monthly_v1', true,
   '{"tier_code":"pro","display_name":"Pro Monthly","billing_interval":"month","price_money":28.99,"seed_source":"google_play_iap_seed"}'::jsonb),
  ('ai.desifaces.pro.yearly', 'yearly', 'subscription', null, 'USD', '', null, 'pro_yearly_v1', true,
   '{"tier_code":"pro","display_name":"Pro Yearly","billing_interval":"year","price_money":289.99,"seed_source":"google_play_iap_seed"}'::jsonb),
  ('ai.desifaces.business.monthly', 'monthly', 'subscription', null, 'USD', '', null, 'business_monthly_v1', true,
   '{"tier_code":"business","display_name":"Business Monthly","billing_interval":"month","price_money":99.99,"seed_source":"google_play_iap_seed"}'::jsonb),
  ('ai.desifaces.business.yearly', 'yearly', 'subscription', null, 'USD', '', null, 'business_yearly_v1', true,
   '{"tier_code":"business","display_name":"Business Yearly","billing_interval":"year","price_money":989.99,"seed_source":"google_play_iap_seed"}'::jsonb)
on conflict (google_product_id, base_plan_id, currency, country_code)
do update set
  product_type = excluded.product_type,
  credits = excluded.credits,
  internal_pack_code = excluded.internal_pack_code,
  internal_plan_code = excluded.internal_plan_code,
  is_active = excluded.is_active,
  metadata_json = excluded.metadata_json,
  updated_at = now();

-- Google Play credit-pack mappings. Mirrors Apple mappings and pricing_credit_packs.code.
insert into public.google_play_iap_product_mappings (
  google_product_id, base_plan_id, product_type, credits, currency, country_code,
  internal_pack_code, internal_plan_code, is_active, metadata_json
) values
  ('ai.desifaces.credits.1000', '', 'consumable', 1000, 'USD', '', 'PACK_USD_1000', null, true,
   '{"best_for":"try it","display_name":"Starter Pack","price_money":9.99,"seed_source":"google_play_iap_seed"}'::jsonb),
  ('ai.desifaces.credits.5000', '', 'consumable', 5000, 'USD', '', 'PACK_USD_5000', null, true,
   '{"tag":"best value","best_for":"most users","display_name":"Value Pack","price_money":39.99,"seed_source":"google_play_iap_seed"}'::jsonb),
  ('ai.desifaces.credits.15000', '', 'consumable', 15000, 'USD', '', 'PACK_USD_15000', null, true,
   '{"best_for":"heavy usage","display_name":"Pro Pack","price_money":99.99,"seed_source":"google_play_iap_seed"}'::jsonb)
on conflict (google_product_id, base_plan_id, currency, country_code)
do update set
  product_type = excluded.product_type,
  credits = excluded.credits,
  internal_pack_code = excluded.internal_pack_code,
  internal_plan_code = excluded.internal_plan_code,
  is_active = excluded.is_active,
  metadata_json = excluded.metadata_json,
  updated_at = now();

commit;
