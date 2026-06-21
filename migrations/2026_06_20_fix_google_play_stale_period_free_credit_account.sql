begin;

-- 1) Expire included-credit lots that are still marked active
--    but have already passed expires_at.
update public.pricing_credit_lots
set status = 'expired',
    updated_at = now()
where bucket_type = 'included'
  and status = 'active'
  and expires_at is not null
  and expires_at <= now();


-- 2) Reactivate/update the deterministic Free restore lot if it already exists.
with free_users as (
  select
    be.user_id,
    greatest(coalesce(be.included_credits_total, 100), 100)::numeric(18,4) as free_cap
  from public.billing_entitlements be
  where lower(coalesce(be.tier_code, '')) = 'free'
    and lower(coalesce(be.plan_code, '')) = 'free'
    and lower(coalesce(be.billing_mode, '')) = 'free'
    and lower(coalesce(be.source, '')) in (
      'google_play_reconciler_stale_period',
      'google_play_reconciler_orphan_entitlement'
    )
    and (be.effective_from is null or be.effective_from <= now())
    and (be.effective_to is null or be.effective_to > now())
)
update public.pricing_credit_lots l
set
  plan_code_at_grant = 'free',
  granted_amount = greatest(coalesce(l.granted_amount, 0), fu.free_cap),
  remaining_amount = greatest(coalesce(l.remaining_amount, 0), fu.free_cap),
  reserved_amount = least(coalesce(l.reserved_amount, 0), fu.free_cap),
  expires_at = null,
  status = 'active',
  metadata_json = case
      when jsonb_typeof(coalesce(l.metadata_json, '{}'::jsonb)) = 'object'
      then coalesce(l.metadata_json, '{}'::jsonb)
      else '{}'::jsonb
    end || jsonb_build_object(
      'source', 'google_play_reconciler_free_restore',
      'reason', 'reactivate_existing_free_restore_lot',
      'plan_code', 'free',
      'tier_code', 'free',
      'included_credit_cap', fu.free_cap,
      'idempotent_repair_at', now()
    ),
  updated_at = now()
from free_users fu
where l.user_id = fu.user_id
  and l.bucket_type = 'included'
  and l.source_type = 'plan_grant'
  and l.source_ref = 'google_play_reconciler_free_restore:' || fu.user_id::text;


-- 3) Insert the deterministic Free restore lot only if it does not exist at all.
with free_users as (
  select
    be.user_id,
    greatest(coalesce(be.included_credits_total, 100), 100)::numeric(18,4) as free_cap
  from public.billing_entitlements be
  where lower(coalesce(be.tier_code, '')) = 'free'
    and lower(coalesce(be.plan_code, '')) = 'free'
    and lower(coalesce(be.billing_mode, '')) = 'free'
    and lower(coalesce(be.source, '')) in (
      'google_play_reconciler_stale_period',
      'google_play_reconciler_orphan_entitlement'
    )
    and (be.effective_from is null or be.effective_from <= now())
    and (be.effective_to is null or be.effective_to > now())
)
insert into public.pricing_credit_lots(
  id,
  billing_account_id,
  user_id,
  bucket_type,
  source_type,
  source_ref,
  plan_code_at_grant,
  granted_amount,
  remaining_amount,
  reserved_amount,
  granted_at,
  expires_at,
  status,
  metadata_json,
  created_at,
  updated_at
)
select
  gen_random_uuid(),
  null::uuid,
  fu.user_id,
  'included',
  'plan_grant',
  'google_play_reconciler_free_restore:' || fu.user_id::text,
  'free',
  fu.free_cap,
  fu.free_cap,
  0,
  now(),
  null,
  'active',
  jsonb_build_object(
    'source', 'google_play_reconciler_free_restore',
    'reason', 'restore_free_included_credits_after_google_play_stale_period',
    'plan_code', 'free',
    'tier_code', 'free',
    'included_credit_cap', fu.free_cap
  ),
  now(),
  now()
from free_users fu
where not exists (
  select 1
  from public.pricing_credit_lots l
  where l.user_id = fu.user_id
    and l.bucket_type = 'included'
    and l.source_type = 'plan_grant'
    and l.source_ref = 'google_play_reconciler_free_restore:' || fu.user_id::text
);


-- 4) Rebuild existing legacy pricing_credit_accounts from active lots.
with target_users as (
  select distinct be.user_id
  from public.billing_entitlements be
  where lower(coalesce(be.tier_code, '')) = 'free'
    and lower(coalesce(be.plan_code, '')) = 'free'
    and lower(coalesce(be.billing_mode, '')) = 'free'
    and lower(coalesce(be.source, '')) in (
      'google_play_reconciler_stale_period',
      'google_play_reconciler_orphan_entitlement'
    )
    and (be.effective_from is null or be.effective_from <= now())
    and (be.effective_to is null or be.effective_to > now())
),
rebuilt as (
  select
    tu.user_id,
    coalesce(sum(
      case
        when l.status = 'active'
         and (l.expires_at is null or l.expires_at > now())
        then coalesce(l.remaining_amount, 0)
        else 0
      end
    ), 0)::bigint as balance_credits,
    coalesce(sum(
      case
        when l.status = 'active'
         and (l.expires_at is null or l.expires_at > now())
        then coalesce(l.reserved_amount, 0)
        else 0
      end
    ), 0)::bigint as reserved_credits
  from target_users tu
  left join public.pricing_credit_lots l
    on l.user_id = tu.user_id
  group by tu.user_id
)
update public.pricing_credit_accounts pca
set balance_credits = rebuilt.balance_credits,
    reserved_credits = rebuilt.reserved_credits,
    settlement_mode = case
      when coalesce(trim(pca.settlement_mode), '') in ('', 'credits') then 'prepaid'
      else pca.settlement_mode
    end,
    updated_at = now()
from rebuilt
where pca.user_id = rebuilt.user_id;


-- 5) Create missing legacy pricing_credit_accounts rows if needed.
with target_users as (
  select distinct be.user_id
  from public.billing_entitlements be
  where lower(coalesce(be.tier_code, '')) = 'free'
    and lower(coalesce(be.plan_code, '')) = 'free'
    and lower(coalesce(be.billing_mode, '')) = 'free'
    and lower(coalesce(be.source, '')) in (
      'google_play_reconciler_stale_period',
      'google_play_reconciler_orphan_entitlement'
    )
    and (be.effective_from is null or be.effective_from <= now())
    and (be.effective_to is null or be.effective_to > now())
),
rebuilt as (
  select
    tu.user_id,
    coalesce(sum(
      case
        when l.status = 'active'
         and (l.expires_at is null or l.expires_at > now())
        then coalesce(l.remaining_amount, 0)
        else 0
      end
    ), 0)::bigint as balance_credits,
    coalesce(sum(
      case
        when l.status = 'active'
         and (l.expires_at is null or l.expires_at > now())
        then coalesce(l.reserved_amount, 0)
        else 0
      end
    ), 0)::bigint as reserved_credits
  from target_users tu
  left join public.pricing_credit_lots l
    on l.user_id = tu.user_id
  group by tu.user_id
)
insert into public.pricing_credit_accounts(
  user_id,
  balance_credits,
  reserved_credits,
  settlement_mode,
  updated_at
)
select
  r.user_id,
  r.balance_credits,
  r.reserved_credits,
  'prepaid',
  now()
from rebuilt r
where not exists (
  select 1
  from public.pricing_credit_accounts pca
  where pca.user_id = r.user_id
);

commit;
