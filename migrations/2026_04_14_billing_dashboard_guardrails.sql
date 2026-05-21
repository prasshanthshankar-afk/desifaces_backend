
-- billing_dashboard_guardrails.sql
-- Launch-safe data-integrity guardrails for billing_entitlements + dashboard pricing snapshot.
--
-- Goals:
-- 1) One canonical active entitlement row per user
-- 2) Plan caps enforced on included credits
-- 3) Optional wallet cap enforced on aggregate balance_credits
-- 4) Dashboard snapshot derives plan truth from active billing_entitlements first
--
-- IMPORTANT:
-- - Current backend code derives plan defaults in entitlement_sync_service.py:
--   free(100 by convention in UI), pro_monthly(500), pro_yearly(6000),
--   business defaults currently mirror pro unless env overrides exist. fileciteturn40file6
-- - Current payments code already resolves current plan from active billing_entitlements. fileciteturn40file15
--
-- Review the seeded cap values below and adjust if your Business caps differ from current defaults.

begin;

create table if not exists pricing_plan_credit_guardrails (
  plan_code text primary key,
  tier_code text not null,
  plan_name text not null,
  included_credit_cap numeric(18,4) not null check (included_credit_cap >= 0),
  wallet_credit_cap numeric(18,4),
  enforce_wallet_cap boolean not null default true,
  allow_topups boolean not null default true,
  is_active boolean not null default true,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into pricing_plan_credit_guardrails (
  plan_code, tier_code, plan_name,
  included_credit_cap, wallet_credit_cap, enforce_wallet_cap, allow_topups, metadata_json
)
values
  ('free',               'free',       'Free',          100,   100,   true,  false, '{"launch_note":"free cap"}'::jsonb),
  ('pro_monthly_v1',     'pro',        'Pro',           500,   500,   true,  true,  '{"launch_note":"pro monthly cap"}'::jsonb),
  ('pro_yearly_v1',      'pro',        'Pro Yearly',   6000,  6000,   true,  true,  '{"launch_note":"pro yearly cap"}'::jsonb),
  ('business_monthly_v1','business',   'Business',      500,   500,   true,  true,  '{"launch_note":"replace if Business monthly cap differs"}'::jsonb),
  ('business_yearly_v1', 'business',   'Business Yearly',6000,6000,   true,  true,  '{"launch_note":"replace if Business yearly cap differs"}'::jsonb),
  ('enterprise_monthly_v1','enterprise','Enterprise',     0,  null,  false, true,  '{"launch_note":"enterprise custom"}'::jsonb),
  ('enterprise_yearly_v1','enterprise','Enterprise Yearly',0,null,   false, true,  '{"launch_note":"enterprise custom"}'::jsonb)
on conflict (plan_code) do update
set tier_code = excluded.tier_code,
    plan_name = excluded.plan_name,
    included_credit_cap = excluded.included_credit_cap,
    wallet_credit_cap = excluded.wallet_credit_cap,
    enforce_wallet_cap = excluded.enforce_wallet_cap,
    allow_topups = excluded.allow_topups,
    metadata_json = excluded.metadata_json,
    updated_at = now();

create or replace function _normalize_plan_code_from_entitlement(
  p_tier_code text,
  p_plan_code text
) returns text
language plpgsql
as $$
declare
  v_plan text := lower(coalesce(trim(p_plan_code), ''));
  v_tier text := lower(coalesce(trim(p_tier_code), ''));
begin
  if v_plan <> '' then
    return v_plan;
  end if;

  if v_tier = 'pro' then
    return 'pro_monthly_v1';
  elsif v_tier = 'business' then
    return 'business_monthly_v1';
  elsif v_tier = 'enterprise' then
    return 'enterprise_monthly_v1';
  end if;

  return 'free';
end;
$$;

create or replace function billing_entitlements_guardrails_biu()
returns trigger
language plpgsql
as $$
declare
  v_plan_code text;
  v_guard pricing_plan_credit_guardrails%rowtype;
begin
  NEW.tier_code := lower(coalesce(trim(NEW.tier_code), 'free'));
  NEW.plan_code := case
    when coalesce(trim(NEW.plan_code), '') = '' then null
    else lower(trim(NEW.plan_code))
  end;

  if NEW.billing_mode is null or trim(NEW.billing_mode) = '' then
    NEW.billing_mode := case when NEW.tier_code = 'free' then 'free' else 'subscription' end;
  else
    NEW.billing_mode := lower(trim(NEW.billing_mode));
  end if;

  if NEW.settlement_mode is null or trim(NEW.settlement_mode) = '' then
    NEW.settlement_mode := 'credits';
  else
    NEW.settlement_mode := lower(trim(NEW.settlement_mode));
  end if;

  v_plan_code := _normalize_plan_code_from_entitlement(NEW.tier_code, NEW.plan_code);

  select *
  into v_guard
  from pricing_plan_credit_guardrails
  where plan_code = v_plan_code
    and is_active = true
  limit 1;

  if not found then
    if NEW.tier_code = 'free' then
      NEW.plan_code := null;
      NEW.billing_mode := 'free';
      NEW.included_credits_total := 0;
      NEW.included_credits_remaining := 0;
      return NEW;
    end if;
    raise exception 'billing_entitlements_guardrails_missing_plan_cap:%', v_plan_code;
  end if;

  if v_guard.tier_code = 'free' or NEW.billing_mode = 'free' then
    NEW.tier_code := 'free';
    NEW.plan_code := null;
    NEW.billing_mode := 'free';
    NEW.included_credits_total := 0;
    NEW.included_credits_remaining := 0;
    NEW.overage_allowed := false;
    NEW.hard_stop_on_insufficient_balance := true;
    return NEW;
  end if;

  NEW.tier_code := v_guard.tier_code;
  NEW.plan_code := v_guard.plan_code;

  if NEW.included_credits_total is null then
    NEW.included_credits_total := v_guard.included_credit_cap;
  end if;
  if NEW.included_credits_total > v_guard.included_credit_cap then
    NEW.included_credits_total := v_guard.included_credit_cap;
  end if;
  if NEW.included_credits_total < 0 then
    NEW.included_credits_total := 0;
  end if;

  if NEW.included_credits_remaining is null then
    NEW.included_credits_remaining := NEW.included_credits_total;
  end if;
  if NEW.included_credits_remaining > NEW.included_credits_total then
    NEW.included_credits_remaining := NEW.included_credits_total;
  end if;
  if NEW.included_credits_remaining < 0 then
    NEW.included_credits_remaining := 0;
  end if;

  if NEW.wallet_topup_allowed is null then
    NEW.wallet_topup_allowed := v_guard.allow_topups;
  end if;

  return NEW;
end;
$$;

drop trigger if exists trg_billing_entitlements_guardrails_biu on billing_entitlements;
create trigger trg_billing_entitlements_guardrails_biu
before insert or update on billing_entitlements
for each row
execute function billing_entitlements_guardrails_biu();

alter table billing_entitlements
  drop constraint if exists billing_entitlements_nonnegative_credits_chk;

alter table billing_entitlements
  add constraint billing_entitlements_nonnegative_credits_chk
  check (
    coalesce(included_credits_total, 0) >= 0
    and coalesce(included_credits_remaining, 0) >= 0
    and coalesce(included_credits_remaining, 0) <= coalesce(included_credits_total, 0)
  );

create unique index if not exists ux_billing_entitlements_one_open_row_per_user
  on billing_entitlements(user_id)
  where effective_to is null;

create or replace function pricing_credit_accounts_wallet_cap_biu()
returns trigger
language plpgsql
as $$
declare
  v_plan_code text;
  v_wallet_cap numeric(18,4);
  v_enforce boolean;
begin
  select _normalize_plan_code_from_entitlement(be.tier_code, be.plan_code)
    into v_plan_code
  from billing_entitlements be
  where be.user_id = NEW.user_id
    and be.effective_from <= now()
    and (be.effective_to is null or be.effective_to > now())
  order by be.effective_from desc, be.updated_at desc
  limit 1;

  if v_plan_code is null then
    v_plan_code := 'free';
  end if;

  select wallet_credit_cap, enforce_wallet_cap
    into v_wallet_cap, v_enforce
  from pricing_plan_credit_guardrails
  where plan_code = v_plan_code
    and is_active = true
  limit 1;

  if coalesce(v_enforce, false) and v_wallet_cap is not null then
    if coalesce(NEW.balance_credits, 0) > v_wallet_cap then
      raise exception 'pricing_credit_accounts_wallet_cap_exceeded:user=% plan=% balance=% cap=%',
        NEW.user_id, v_plan_code, NEW.balance_credits, v_wallet_cap;
    end if;
  end if;

  if NEW.balance_credits is not null and NEW.balance_credits < 0 then
    raise exception 'pricing_credit_accounts_negative_balance:user=% balance=%',
      NEW.user_id, NEW.balance_credits;
  end if;

  if NEW.reserved_credits is not null and NEW.reserved_credits < 0 then
    raise exception 'pricing_credit_accounts_negative_reserved:user=% reserved=%',
      NEW.user_id, NEW.reserved_credits;
  end if;

  return NEW;
end;
$$;

drop trigger if exists trg_pricing_credit_accounts_wallet_cap_biu on pricing_credit_accounts;
create trigger trg_pricing_credit_accounts_wallet_cap_biu
before insert or update on pricing_credit_accounts
for each row
execute function pricing_credit_accounts_wallet_cap_biu();

alter table pricing_credit_accounts
  drop constraint if exists pricing_credit_accounts_nonnegative_chk;

alter table pricing_credit_accounts
  add constraint pricing_credit_accounts_nonnegative_chk
  check (
    coalesce(balance_credits, 0) >= 0
    and coalesce(reserved_credits, 0) >= 0
  );

create or replace view v_dashboard_pricing_snapshot as
with users_union as (
  select user_id from pricing_credit_accounts
  union
  select user_id from billing_entitlements
),
active_entitlement as (
  select distinct on (be.user_id)
    be.user_id,
    lower(coalesce(be.tier_code, 'free')) as tier_code,
    _normalize_plan_code_from_entitlement(be.tier_code, be.plan_code) as plan_code,
    lower(coalesce(be.billing_mode, case when lower(coalesce(be.tier_code, 'free')) = 'free' then 'free' else 'subscription' end)) as billing_mode,
    lower(coalesce(be.settlement_mode, 'credits')) as settlement_mode,
    coalesce(be.included_credits_total, 0)::numeric(18,4) as included_credits_total,
    coalesce(be.included_credits_remaining, 0)::numeric(18,4) as included_credits_remaining,
    be.source,
    be.updated_at,
    be.metadata_json
  from billing_entitlements be
  where be.effective_from <= now()
    and (be.effective_to is null or be.effective_to > now())
  order by be.user_id, be.effective_from desc, be.updated_at desc
),
acct as (
  select
    pca.user_id,
    coalesce(pca.balance_credits, 0)::numeric(18,4) as balance_credits,
    coalesce(pca.reserved_credits, 0)::numeric(18,4) as reserved_credits,
    pca.updated_at
  from pricing_credit_accounts pca
),
joined as (
  select
    u.user_id,
    coalesce(ae.tier_code, 'free') as tier_code,
    coalesce(ae.plan_code, 'free') as plan_code,
    coalesce(ae.billing_mode, 'free') as billing_mode,
    coalesce(ae.settlement_mode, 'credits') as settlement_mode,
    coalesce(ae.included_credits_total, 0)::numeric(18,4) as included_credits_total,
    coalesce(ae.included_credits_remaining, 0)::numeric(18,4) as included_credits_remaining,
    coalesce(ac.balance_credits, 0)::numeric(18,4) as balance_credits,
    coalesce(ac.reserved_credits, 0)::numeric(18,4) as reserved_credits,
    ae.source as entitlement_source,
    ae.updated_at as entitlement_updated_at,
    ac.updated_at as account_updated_at,
    ae.metadata_json as entitlement_metadata
  from users_union u
  left join active_entitlement ae on ae.user_id = u.user_id
  left join acct ac on ac.user_id = u.user_id
)
select
  j.user_id,
  jsonb_build_object(
    'source', coalesce(j.entitlement_source, 'billing_entitlements'),
    'plan_name',
      case
        when j.plan_code = 'free' then 'Free'
        when j.plan_code = 'pro_monthly_v1' then 'Pro'
        when j.plan_code = 'pro_yearly_v1' then 'Pro Yearly'
        when j.plan_code = 'business_monthly_v1' then 'Business'
        when j.plan_code = 'business_yearly_v1' then 'Business Yearly'
        when j.plan_code = 'enterprise_monthly_v1' then 'Enterprise'
        when j.plan_code = 'enterprise_yearly_v1' then 'Enterprise Yearly'
        else initcap(replace(j.plan_code, '_', ' '))
      end,
    'tier_code', j.tier_code,
    'plan_code', j.plan_code,
    'billing_mode', j.billing_mode,
    'settlement_mode', j.settlement_mode,
    'credit_cap', j.included_credits_total
  ) as plan_summary_json,
  jsonb_build_object(
    'source', 'pricing_credit_accounts+billing_entitlements',
    'updated_at', coalesce(j.account_updated_at, j.entitlement_updated_at),
    'available_credits', j.balance_credits,
    'reserved_credits', j.reserved_credits,
    'included_credits_total', j.included_credits_total,
    'included_credits_remaining', j.included_credits_remaining
  ) as pricing_summary_json,
  jsonb_build_object(
    'source', 'billing_entitlements',
    'used_credits', greatest(j.included_credits_total - j.included_credits_remaining, 0),
    'usage_percent',
      case
        when j.included_credits_total > 0
        then round((greatest(j.included_credits_total - j.included_credits_remaining, 0) / j.included_credits_total) * 100, 2)
        else 0
      end
  ) as usage_summary_json,
  jsonb_build_object(
    'used_credits', greatest(j.included_credits_total - j.included_credits_remaining, 0),
    'usage_percent',
      case
        when j.included_credits_total > 0
        then round((greatest(j.included_credits_total - j.included_credits_remaining, 0) / j.included_credits_total) * 100, 2)
        else 0
      end,
    'reserved_credits', j.reserved_credits,
    'available_credits', j.balance_credits
  ) as usage_json
from joined j;

commit;
