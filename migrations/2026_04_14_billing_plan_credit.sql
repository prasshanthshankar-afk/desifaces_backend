-- billing_plan_credit_source_of_truth.sql
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
  plan_code, tier_code, plan_name, included_credit_cap, wallet_credit_cap, enforce_wallet_cap, allow_topups, metadata_json
)
values
  ('free', 'free', 'Free', 100, 100, true, false, '{"source":"launch_ladder"}'::jsonb),
  ('pro_monthly_v1', 'pro', 'Pro', 500, 500, true, true, '{"source":"launch_ladder"}'::jsonb),
  ('pro_yearly_v1', 'pro', 'Pro Yearly', 6000, 6000, true, true, '{"source":"launch_ladder"}'::jsonb),
  ('business_monthly_v1', 'business', 'Business', 2000, 2000, true, true, '{"source":"launch_ladder"}'::jsonb),
  ('business_yearly_v1', 'business', 'Business Yearly', 24000, 24000, true, true, '{"source":"launch_ladder"}'::jsonb),
  ('enterprise_monthly_v1', 'enterprise', 'Enterprise', 0, null, false, true, '{"source":"launch_ladder"}'::jsonb),
  ('enterprise_yearly_v1', 'enterprise', 'Enterprise Yearly', 0, null, false, true, '{"source":"launch_ladder"}'::jsonb)
on conflict (plan_code) do update
set tier_code = excluded.tier_code,
    plan_name = excluded.plan_name,
    included_credit_cap = excluded.included_credit_cap,
    wallet_credit_cap = excluded.wallet_credit_cap,
    enforce_wallet_cap = excluded.enforce_wallet_cap,
    allow_topups = excluded.allow_topups,
    metadata_json = excluded.metadata_json,
    updated_at = now();

update billing_entitlements
set included_credits_total = 2000,
    included_credits_remaining = least(coalesce(included_credits_remaining, 0), 2000),
    updated_at = now()
where plan_code = 'business_monthly_v1'
  and included_credits_total <> 2000;

update billing_entitlements
set included_credits_total = 24000,
    included_credits_remaining = least(coalesce(included_credits_remaining, 0), 24000),
    updated_at = now()
where plan_code = 'business_yearly_v1'
  and included_credits_total <> 24000;

commit;



 