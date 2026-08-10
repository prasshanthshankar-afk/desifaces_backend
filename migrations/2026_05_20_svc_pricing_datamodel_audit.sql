-- DesiFaces svc-pricing datamodel audit v3
-- Correctly checks owner-scoped credit-lot idempotency.
\pset pager off
\echo '============================================================'
\echo 'A. CRITICAL TABLE PRESENCE'
\echo '============================================================'
select x.table_name,
       case when to_regclass('public.' || x.table_name) is null then 'MISSING' else 'OK' end as status,
       x.purpose
from (values
  ('pricing_plan_transition_events','provider-neutral plan transition idempotency/audit'),
  ('pricing_billing_provider_bindings','explicit active billing provider ownership'),
  ('payment_plan_subscriptions','provider subscription state'),
  ('billing_entitlements','canonical billing entitlement'),
  ('pricing_user_entitlements','product entitlement tier'),
  ('pricing_credit_lots','spendable credit source of truth'),
  ('pricing_credit_accounts','cached balance summary')
) as x(table_name, purpose)
order by status desc, table_name;

\echo '============================================================'
\echo 'B. CRITICAL IDEMPOTENCY / OWNERSHIP INDEXES'
\echo '============================================================'
with checks(check_name, present, expected) as (
  values
    (
      'transition event idempotency key',
      exists (
        select 1 from pg_indexes
        where schemaname='public'
          and tablename='pricing_plan_transition_events'
          and indexdef ilike '%unique%'
          and indexdef ilike '%idempotency_key%'
      ),
      'unique pricing_plan_transition_events(idempotency_key)'
    ),
    (
      'active billing provider binding primary key',
      exists (
        select 1 from pg_constraint con
        join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname='public'
          and c.relname='pricing_billing_provider_bindings'
          and con.contype in ('p','u')
          and pg_get_constraintdef(con.oid) ilike '%user_id%'
      ),
      'primary key / unique(user_id)'
    ),
    (
      'payment subscriptions provider-scoped identity',
      exists (
        select 1 from pg_indexes
        where schemaname='public'
          and tablename='payment_plan_subscriptions'
          and indexdef ilike '%unique%'
          and indexdef ilike '%gateway_provider%'
          and indexdef ilike '%gateway_subscription_id%'
      ),
      'unique(gateway_provider,gateway_subscription_id) partial index'
    ),
    (
      'credit lot user-owner idempotency',
      exists (
        select 1 from pg_indexes
        where schemaname='public'
          and tablename='pricing_credit_lots'
          and indexdef ilike '%unique%'
          and indexdef ilike '%user_id%'
          and indexdef ilike '%bucket_type%'
          and indexdef ilike '%source_type%'
          and indexdef ilike '%source_ref%'
      ),
      'unique(user_id,bucket_type,source_type,source_ref) partial index'
    ),
    (
      'credit lot billing-account-owner idempotency',
      exists (
        select 1 from pg_indexes
        where schemaname='public'
          and tablename='pricing_credit_lots'
          and indexdef ilike '%unique%'
          and indexdef ilike '%billing_account_id%'
          and indexdef ilike '%bucket_type%'
          and indexdef ilike '%source_type%'
          and indexdef ilike '%source_ref%'
      ),
      'unique(billing_account_id,bucket_type,source_type,source_ref) partial index'
    )
)
select check_name, case when present then 'PASS' else 'FAIL' end as status, expected
from checks
order by status, check_name;

\echo '============================================================'
\echo 'C. ACTIVE SUBSCRIPTION CONFLICTS'
\echo '============================================================'
select user_id,
       count(*) as active_subscription_rows,
       jsonb_agg(jsonb_build_object(
         'provider', gateway_provider,
         'provider_sub_id', gateway_subscription_id,
         'price_id', gateway_price_id,
         'plan_code', plan_code,
         'subscription_state', subscription_state,
         'entitlement_state', entitlement_state,
         'cancel_at_period_end', cancel_at_period_end,
         'current_period_end', current_period_end
       ) order by updated_at desc) as active_rows
from public.payment_plan_subscriptions
where entitlement_state in ('active','grace')
  and subscription_state in ('trialing','active','past_due','unpaid','paused')
group by user_id
having count(*) > 1
order by count(*) desc, user_id
limit 100;

\echo '============================================================'
\echo 'D. BILLING ENTITLEMENT METADATA MUST BE JSON OBJECT'
\echo '============================================================'
select user_id, plan_code, jsonb_typeof(metadata_json) as metadata_type, updated_at
from public.billing_entitlements
where metadata_json is not null
  and jsonb_typeof(metadata_json) <> 'object'
order by updated_at desc
limit 100;

\echo '============================================================'
\echo 'E. ACCOUNT CACHE VS ACTIVE LOTS MISMATCH'
\echo '============================================================'
with lot_sums as (
  select user_id,
         coalesce(sum(remaining_amount) filter (where status='active'), 0)::bigint as lot_balance,
         coalesce(sum(reserved_amount) filter (where status='active'), 0)::bigint as lot_reserved
  from public.pricing_credit_lots
  where user_id is not null
  group by user_id
)
select a.user_id,
       a.balance_credits as account_balance,
       coalesce(l.lot_balance,0) as lot_balance,
       a.reserved_credits as account_reserved,
       coalesce(l.lot_reserved,0) as lot_reserved,
       a.updated_at
from public.pricing_credit_accounts a
left join lot_sums l on l.user_id = a.user_id
where a.balance_credits <> coalesce(l.lot_balance,0)
   or a.reserved_credits <> coalesce(l.lot_reserved,0)
order by a.updated_at desc
limit 200;

\echo '============================================================'
\echo 'F. BILLING VS PRODUCT ENTITLEMENT TIER MISMATCH'
\echo '============================================================'
select be.user_id,
       be.tier_code as billing_tier,
       pe.tier_code as product_tier,
       be.plan_code,
       be.updated_at as billing_updated_at,
       pe.effective_from as product_effective_from
from public.billing_entitlements be
left join public.pricing_user_entitlements pe on pe.user_id = be.user_id
where coalesce(lower(be.tier_code),'') <> coalesce(lower(pe.tier_code),'')
order by be.updated_at desc
limit 200;

\echo '============================================================'
\echo 'G. PLAN TOTAL VS LIVE INCLUDED LOTS MISMATCH'
\echo '============================================================'
with included_lots as (
  select user_id,
         coalesce(sum(granted_amount) filter (where status='active' and bucket_type='included'),0) as included_granted_active,
         coalesce(sum(remaining_amount) filter (where status='active' and bucket_type='included'),0) as included_remaining_active,
         coalesce(sum(reserved_amount) filter (where status='active' and bucket_type='included'),0) as included_reserved_active
  from public.pricing_credit_lots
  where user_id is not null
  group by user_id
)
select be.user_id,
       be.plan_code,
       be.included_credits_total,
       be.included_credits_remaining,
       coalesce(l.included_granted_active,0) as active_included_granted,
       coalesce(l.included_remaining_active,0) as active_included_remaining,
       coalesce(l.included_reserved_active,0) as active_included_reserved,
       (be.included_credits_total - coalesce(l.included_remaining_active,0) - coalesce(l.included_reserved_active,0)) as inferred_used_from_active_lots,
       be.updated_at
from public.billing_entitlements be
left join included_lots l on l.user_id = be.user_id
where be.settlement_mode = 'credits'
  and (
    be.included_credits_remaining <> coalesce(l.included_remaining_active,0)
    or be.included_credits_total < coalesce(l.included_remaining_active,0) + coalesce(l.included_reserved_active,0)
  )
order by be.updated_at desc
limit 200;

\echo '============================================================'
\echo 'H. HOT QUERY EXPLAIN FOR USER105'
\echo '============================================================'
explain (analyze, buffers)
select *
from public.v_pricing_account_overview
where user_id = '71ba1ee1-40e5-4641-a896-d3410b0fc453'::uuid;
