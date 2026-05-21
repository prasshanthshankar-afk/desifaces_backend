-- File: /mnt/data/pricing_stage1_additive_foundation.sql
-- Purpose:
--   Additive database foundation for the unified pricing/billing model.
--
-- Scope:
--   1) Add credit lots table for included/purchased/promo balances
--   2) Add allocation breakdown support to pricing_credit_reservations
--   3) Add pricing_integrity_issues table for reconciliation/alerts
--   4) Add v_pricing_account_overview read model for canonical UI/API usage
--
-- Important:
--   - This migration is additive and does NOT remove existing columns/tables
--   - Existing runtime code will still need API/service changes to use these tables
--   - This is the DB foundation, not the full end-to-end rollout by itself

begin;

set local lock_timeout = '5s';
set local statement_timeout = '120s';

-- ============================================================================
-- 1) Credit lots
-- ============================================================================

create table if not exists pricing_credit_lots (
    id uuid primary key default gen_random_uuid(),
    billing_account_id uuid null,
    user_id uuid null,

    bucket_type text not null check (bucket_type in ('included', 'purchased', 'promo')),
    source_type text not null check (
        source_type in ('plan_grant', 'topup', 'admin_adjustment', 'migration', 'promo_grant')
    ),
    source_ref text null,
    plan_code_at_grant text null,

    granted_amount numeric(18,4) not null default 0,
    remaining_amount numeric(18,4) not null default 0,
    reserved_amount numeric(18,4) not null default 0,

    granted_at timestamptz not null default now(),
    expires_at timestamptz null,

    status text not null default 'active' check (
        status in ('active', 'expired', 'consumed', 'voided')
    ),

    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    constraint pricing_credit_lots_owner_check
        check (
            billing_account_id is not null
            or user_id is not null
        ),

    constraint pricing_credit_lots_amounts_nonnegative
        check (
            granted_amount >= 0
            and remaining_amount >= 0
            and reserved_amount >= 0
        )
);

create index if not exists idx_pricing_credit_lots_user_active
    on pricing_credit_lots (user_id, status, bucket_type, expires_at);

create index if not exists idx_pricing_credit_lots_billing_account_active
    on pricing_credit_lots (billing_account_id, status, bucket_type, expires_at);

create index if not exists idx_pricing_credit_lots_source
    on pricing_credit_lots (source_type, source_ref);

-- ============================================================================
-- 2) Reservation allocation breakdown support
-- ============================================================================

alter table pricing_credit_reservations
    add column if not exists allocations_json jsonb not null default '{"allocations":[]}'::jsonb;

alter table pricing_credit_reservations
    add column if not exists funding_summary_json jsonb not null default '{}'::jsonb;

comment on column pricing_credit_reservations.allocations_json is
'Exact reservation allocations by lot/bucket, e.g. included/purchased/postpaid splits.';

comment on column pricing_credit_reservations.funding_summary_json is
'Summary of funding source usage for preview/reserve/commit display.';

-- ============================================================================
-- 3) Integrity issue tracking
-- ============================================================================

create table if not exists pricing_integrity_issues (
    id uuid primary key default gen_random_uuid(),
    user_id uuid null,
    billing_account_id uuid null,
    issue_code text not null,
    severity text not null check (severity in ('info', 'warning', 'error', 'critical')),
    details_json jsonb not null default '{}'::jsonb,
    detected_at timestamptz not null default now(),
    resolved_at timestamptz null,
    resolution_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists idx_pricing_integrity_issues_open_by_user
    on pricing_integrity_issues (user_id, resolved_at, severity);

create index if not exists idx_pricing_integrity_issues_open_by_billing_account
    on pricing_integrity_issues (billing_account_id, resolved_at, severity);

-- ============================================================================
-- 4) Canonical overview read model
--    This is intentionally conservative and additive. It summarizes:
--      - active entitlement truth
--      - summarized lots
--      - existing wallet/account row for compatibility visibility
-- ============================================================================

drop view if exists v_pricing_account_overview;

create view v_pricing_account_overview as
with active_entitlement as (
    select distinct on (be.user_id)
        be.user_id,
        be.tier_code,
        coalesce(nullif(be.plan_code, ''), be.tier_code) as plan_code,
        be.billing_mode,
        be.settlement_mode,
        be.included_credits_total,
        be.included_credits_remaining,
        be.effective_from,
        be.effective_to,
        be.updated_at,
        be.source
    from billing_entitlements be
    where be.effective_to is null
    order by be.user_id, be.effective_from desc nulls last, be.updated_at desc nulls last
),
lot_summary as (
    select
        coalesce(l.user_id, bam.user_id) as user_id,
        sum(case when l.bucket_type = 'included' and l.status = 'active' then l.remaining_amount else 0 end) as included_available,
        sum(case when l.bucket_type = 'included' and l.status = 'active' then l.reserved_amount else 0 end) as included_reserved,
        sum(case when l.bucket_type = 'purchased' and l.status = 'active' then l.remaining_amount else 0 end) as purchased_available,
        sum(case when l.bucket_type = 'purchased' and l.status = 'active' then l.reserved_amount else 0 end) as purchased_reserved,
        sum(case when l.bucket_type = 'promo' and l.status = 'active' then l.remaining_amount else 0 end) as promo_available,
        sum(case when l.bucket_type = 'promo' and l.status = 'active' then l.reserved_amount else 0 end) as promo_reserved
    from pricing_credit_lots l
    left join pricing_billing_account_members bam
      on bam.billing_account_id = l.billing_account_id
    group by coalesce(l.user_id, bam.user_id)
),
account_summary as (
    select
        pca.user_id,
        pca.balance_credits,
        pca.reserved_credits,
        pca.billing_account_id,
        pca.settlement_mode as account_settlement_mode,
        pca.updated_at as account_updated_at
    from pricing_credit_accounts pca
)
select
    coalesce(ae.user_id, ls.user_id, ac.user_id) as user_id,

    jsonb_build_object(
        'source', coalesce(ae.source, 'none'),
        'tier_code', ae.tier_code,
        'plan_code', ae.plan_code,
        'billing_mode', ae.billing_mode,
        'settlement_mode', ae.settlement_mode,
        'included_credits_total', coalesce(ae.included_credits_total, 0),
        'included_credits_remaining', coalesce(ae.included_credits_remaining, 0),
        'effective_from', ae.effective_from,
        'updated_at', ae.updated_at
    ) as plan_json,

    jsonb_build_object(
        'included_available', coalesce(ls.included_available, 0),
        'included_reserved', coalesce(ls.included_reserved, 0),
        'purchased_available', coalesce(ls.purchased_available, 0),
        'purchased_reserved', coalesce(ls.purchased_reserved, 0),
        'promo_available', coalesce(ls.promo_available, 0),
        'promo_reserved', coalesce(ls.promo_reserved, 0),
        'total_spendable',
            coalesce(ls.included_available, 0)
          + coalesce(ls.purchased_available, 0)
          + coalesce(ls.promo_available, 0)
    ) as lots_json,

    jsonb_build_object(
        'legacy_balance_credits', coalesce(ac.balance_credits, 0),
        'legacy_reserved_credits', coalesce(ac.reserved_credits, 0),
        'billing_account_id', ac.billing_account_id,
        'settlement_mode', ac.account_settlement_mode,
        'updated_at', ac.account_updated_at
    ) as legacy_account_json

from active_entitlement ae
full outer join lot_summary ls
    on ls.user_id = ae.user_id
full outer join account_summary ac
    on ac.user_id = coalesce(ae.user_id, ls.user_id);

commit;

-- ============================================================================
-- Verification queries
-- ============================================================================

-- 1) New tables exist
select table_name
from information_schema.tables
where table_schema = 'public'
  and table_name in ('pricing_credit_lots', 'pricing_integrity_issues')
order by table_name;

-- 2) New columns exist
select table_name, column_name
from information_schema.columns
where table_schema = 'public'
  and table_name = 'pricing_credit_reservations'
  and column_name in ('allocations_json', 'funding_summary_json')
order by table_name, column_name;

-- 3) New view exists
select table_name
from information_schema.views
where table_schema = 'public'
  and table_name = 'v_pricing_account_overview';

-- 4) Sample overview for user1
select *
from v_pricing_account_overview
where user_id = 'dccb05ca-abb9-4f08-a01b-ae7836d9ebf8';
