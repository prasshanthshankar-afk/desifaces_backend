BEGIN;

ALTER TABLE core.users
DROP CONSTRAINT IF EXISTS users_tier_check;

ALTER TABLE core.users
ADD CONSTRAINT users_tier_check
CHECK (tier = ANY (ARRAY['free'::text, 'pro'::text, 'business'::text, 'enterprise'::text]));

COMMIT;

BEGIN;

create temporary table _seed_users (
  email text primary key,
  tier_code text not null,
  plan_code text,
  billing_mode text not null,
  settlement_mode text not null,
  included_credits_total numeric not null,
  included_credits_remaining numeric not null,
  overage_allowed boolean not null,
  wallet_topup_allowed boolean not null,
  hard_stop_on_insufficient_balance boolean not null,
  wallet_balance numeric
) on commit drop;

insert into _seed_users (
  email, tier_code, plan_code, billing_mode, settlement_mode,
  included_credits_total, included_credits_remaining,
  overage_allowed, wallet_topup_allowed, hard_stop_on_insufficient_balance,
  wallet_balance
)
values
  ('user1@desifaces.ai', 'free',       null,                     'free',         'credits', 0,    0,    false, true,  true,  100),
  ('user4@desifaces.ai', 'pro',        'pro_monthly_v1',         'subscription', 'credits', 500,  500,  false, true,  true,  1000),
  ('user3@desifaces.ai', 'business',   'business_monthly_v1',    'subscription', 'credits', 1500, 1500, false, true,  true,  2500),
  ('user2@desifaces.ai', 'enterprise', 'enterprise_contract_v1', 'postpaid',     'money',   0,    0,    true,  true,  false, null);

-- optional: align core.users.tier only for values allowed by schema
update core.users u
set tier = s.tier_code
from _seed_users s
where lower(u.email) = lower(s.email)
  and s.tier_code in ('free', 'pro', 'enterprise');

insert into pricing_user_entitlements (
  user_id,
  tier_code,
  billing_account_id,
  metadata_json
)
select
  u.id,
  s.tier_code,
  null,
  jsonb_build_object(
    'source', 'e2e_seed',
    'email', s.email,
    'settlement_mode', s.settlement_mode
  )
from _seed_users s
join core.users u on lower(u.email) = lower(s.email)
on conflict (user_id)
do update set
  tier_code = excluded.tier_code,
  billing_account_id = excluded.billing_account_id,
  metadata_json = excluded.metadata_json,
  effective_from = now();

insert into billing_entitlements (
  user_id,
  tier_code,
  plan_code,
  billing_mode,
  settlement_mode,
  included_credits_total,
  included_credits_remaining,
  overage_allowed,
  wallet_topup_allowed,
  hard_stop_on_insufficient_balance,
  source,
  metadata_json,
  updated_at
)
select
  u.id,
  s.tier_code,
  s.plan_code,
  s.billing_mode,
  s.settlement_mode,
  s.included_credits_total,
  s.included_credits_remaining,
  s.overage_allowed,
  s.wallet_topup_allowed,
  s.hard_stop_on_insufficient_balance,
  'e2e_seed',
  jsonb_build_object('source', 'e2e_seed', 'email', s.email),
  now()
from _seed_users s
join core.users u on lower(u.email) = lower(s.email)
on conflict (user_id)
do update set
  tier_code = excluded.tier_code,
  plan_code = excluded.plan_code,
  billing_mode = excluded.billing_mode,
  settlement_mode = excluded.settlement_mode,
  included_credits_total = excluded.included_credits_total,
  included_credits_remaining = excluded.included_credits_remaining,
  overage_allowed = excluded.overage_allowed,
  wallet_topup_allowed = excluded.wallet_topup_allowed,
  hard_stop_on_insufficient_balance = excluded.hard_stop_on_insufficient_balance,
  source = excluded.source,
  metadata_json = excluded.metadata_json,
  updated_at = now();

insert into pricing_credit_accounts (
  user_id,
  balance_credits,
  reserved_credits,
  updated_at
)
select
  u.id,
  s.wallet_balance,
  0,
  now()
from _seed_users s
join core.users u on lower(u.email) = lower(s.email)
where s.wallet_balance is not null
on conflict (user_id)
do update set
  balance_credits = excluded.balance_credits,
  reserved_credits = 0,
  updated_at = now();

COMMIT;