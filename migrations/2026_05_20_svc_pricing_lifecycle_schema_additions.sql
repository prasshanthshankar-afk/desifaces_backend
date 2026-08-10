-- DesiFaces svc-pricing lifecycle schema additions v3
-- Provider-neutral, atomic subscription lifecycle support.
-- Safe to run repeatedly. Do not wrap this file in BEGIN/COMMIT because CREATE INDEX CONCURRENTLY is used.

-- 1) Provider-neutral transition idempotency/audit table.
create table if not exists public.pricing_plan_transition_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  idempotency_key text not null,
  provider text not null,
  provider_event_id text,
  provider_subscription_id text,
  provider_customer_id text,
  provider_price_id text,
  event_type text not null,
  transition_type text not null default 'plan_change',
  effective_mode text not null default 'immediate',
  old_plan_code text,
  new_plan_code text,
  old_tier_code text,
  new_tier_code text,
  transition_status text not null default 'applied',
  computation_json jsonb not null default '{}'::jsonb,
  before_json jsonb not null default '{}'::jsonb,
  after_json jsonb not null default '{}'::jsonb,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint pricing_plan_transition_events_idempotency_key_nonempty_chk check (length(trim(idempotency_key)) > 0),
  constraint pricing_plan_transition_events_provider_nonempty_chk check (length(trim(provider)) > 0),
  constraint pricing_plan_transition_events_status_chk check (transition_status in ('pending','applied','scheduled','ignored','failed','reversed')),
  constraint pricing_plan_transition_events_effective_mode_chk check (effective_mode in ('immediate','period_end','renewal','manual','no_credit_change'))
);

create unique index concurrently if not exists ux_pricing_plan_transition_events_idempotency_key
on public.pricing_plan_transition_events (idempotency_key);

create index concurrently if not exists ix_pricing_plan_transition_events_user_created
on public.pricing_plan_transition_events (user_id, created_at desc);

create index concurrently if not exists ix_pricing_plan_transition_events_provider_subscription
on public.pricing_plan_transition_events (provider, provider_subscription_id, created_at desc)
where provider_subscription_id is not null and trim(provider_subscription_id) <> '';

create index concurrently if not exists ix_pricing_plan_transition_events_provider_event
on public.pricing_plan_transition_events (provider, provider_event_id)
where provider_event_id is not null and trim(provider_event_id) <> '';

-- 2) Explicit active billing-provider ownership table.
create table if not exists public.pricing_billing_provider_bindings (
  user_id uuid primary key,
  active_provider text not null,
  active_provider_subscription_id text,
  active_provider_customer_id text,
  active_provider_price_id text,
  active_plan_code text not null,
  active_tier_code text not null,
  binding_state text not null default 'active',
  source text not null default 'system',
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint pricing_billing_provider_bindings_provider_nonempty_chk check (length(trim(active_provider)) > 0),
  constraint pricing_billing_provider_bindings_plan_nonempty_chk check (length(trim(active_plan_code)) > 0),
  constraint pricing_billing_provider_bindings_tier_nonempty_chk check (length(trim(active_tier_code)) > 0),
  constraint pricing_billing_provider_bindings_state_chk check (binding_state in ('active','pending','inactive','superseded'))
);

create index concurrently if not exists ix_pricing_billing_provider_bindings_provider_subscription
on public.pricing_billing_provider_bindings (active_provider, active_provider_subscription_id)
where active_provider_subscription_id is not null and trim(active_provider_subscription_id) <> '';

create index concurrently if not exists ix_pricing_billing_provider_bindings_plan
on public.pricing_billing_provider_bindings (active_plan_code, binding_state);

-- 3) Provider subscription idempotency. This is provider-scoped, not globally subscription-id scoped.
create unique index concurrently if not exists ux_payment_plan_subscriptions_provider_subscription
on public.payment_plan_subscriptions (gateway_provider, gateway_subscription_id)
where gateway_subscription_id is not null and trim(gateway_subscription_id) <> '';

create index concurrently if not exists ix_payment_plan_subscriptions_user_active_lookup
on public.payment_plan_subscriptions (user_id, entitlement_state, subscription_state, current_period_end desc, updated_at desc);

-- 4) Owner-scoped credit-lot idempotency. Do not use global unique(source_type, source_ref).
create unique index concurrently if not exists ux_pricing_credit_lots_user_source_ref
on public.pricing_credit_lots (user_id, bucket_type, source_type, source_ref)
where user_id is not null
  and source_ref is not null
  and trim(source_ref) <> '';

create unique index concurrently if not exists ux_pricing_credit_lots_billing_account_source_ref
on public.pricing_credit_lots (billing_account_id, bucket_type, source_type, source_ref)
where billing_account_id is not null
  and source_ref is not null
  and trim(source_ref) <> '';

create index concurrently if not exists ix_pricing_credit_lots_user_active_hot
on public.pricing_credit_lots (user_id, status, bucket_type, expires_at)
where user_id is not null;

create index concurrently if not exists ix_pricing_credit_lots_billing_account_active_hot
on public.pricing_credit_lots (billing_account_id, status, bucket_type, expires_at)
where billing_account_id is not null;

-- 5) Provider audit hot indexes.
create index concurrently if not exists ix_apple_iap_transactions_user_created
on public.apple_iap_transactions (user_id, created_at desc);

create index concurrently if not exists ix_apple_iap_transactions_original_transaction
on public.apple_iap_transactions (original_transaction_id)
where original_transaction_id is not null and trim(original_transaction_id) <> '';

create index concurrently if not exists ix_payment_wallet_orders_user_idempotency
on public.payment_wallet_orders (user_id, idempotency_key)
where idempotency_key is not null and trim(idempotency_key) <> '';
