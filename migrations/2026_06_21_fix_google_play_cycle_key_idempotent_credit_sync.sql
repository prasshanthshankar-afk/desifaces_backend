-- Fix Google Play subscription included-credit cycle sync.
--
-- Problem:
-- Google Play subscription restore/renewal can keep the same purchase token and
-- original startTime while expiryTime/current_period_end advances. The old
-- df_sync_subscription_cycle_credits function used current_period_start as the
-- cycle key, causing duplicate source_ref conflicts and preventing renewed
-- Google periods from getting a clean included-credit lot.
--
-- Fix:
-- - Use current_period_end as the cycle key for google_play subscriptions.
-- - Keep repeated confirms for the same period idempotent.
-- - Expire active included lots whose expires_at is already in the past.
-- - Preserve purchased/top-up lots.
--
-- Safe to re-run: CREATE OR REPLACE FUNCTION.
CREATE OR REPLACE FUNCTION public.df_sync_subscription_cycle_credits(p_user_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
AS $function$
declare
  v_ent_id uuid;
  v_ent_plan_code text;
  v_ent_tier_code text;
  v_ent_included_total numeric := 0;
  v_ent_settlement_mode text;
  v_ent_billing_mode text;

  v_sub_id uuid;
  v_sub_provider text;
  v_sub_gateway_id text;
  v_sub_plan_code text;
  v_sub_entitlement_state text;
  v_period_start timestamptz;
  v_period_end timestamptz;

  v_plan_code text;
  v_tier_code text;
  v_cycle_key text;
  v_source_ref text;

  v_existing_lot_id uuid;
  v_inserted_lot_id uuid;
  v_expired_count integer := 0;
  v_remaining numeric := 0;
  v_reserved numeric := 0;
  v_granted numeric := 0;
begin
  -- Serialize per user so repeated native-IAP callbacks cannot double-grant.
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  select
    e.id,
    coalesce(e.plan_code, 'free'),
    coalesce(e.tier_code, 'free'),
    coalesce(e.included_credits_total, 0),
    coalesce(e.settlement_mode, 'credits'),
    coalesce(e.billing_mode, 'free')
  into
    v_ent_id,
    v_ent_plan_code,
    v_ent_tier_code,
    v_ent_included_total,
    v_ent_settlement_mode,
    v_ent_billing_mode
  from public.billing_entitlements e
  where e.user_id = p_user_id
    and e.effective_from <= now()
    and (e.effective_to is null or e.effective_to > now())
  order by e.updated_at desc nulls last, e.created_at desc nulls last
  limit 1;

  if v_ent_id is null then
    return jsonb_build_object(
      'ok', false,
      'action', 'no_active_billing_entitlement',
      'user_id', p_user_id
    );
  end if;

  select
    s.id,
    coalesce(s.gateway_provider, ''),
    coalesce(s.gateway_subscription_id, ''),
    coalesce(s.plan_code, ''),
    coalesce(s.entitlement_state, ''),
    s.current_period_start,
    s.current_period_end
  into
    v_sub_id,
    v_sub_provider,
    v_sub_gateway_id,
    v_sub_plan_code,
    v_sub_entitlement_state,
    v_period_start,
    v_period_end
  from public.payment_plan_subscriptions s
  where s.user_id = p_user_id
    and coalesce(s.entitlement_state, '') in ('active', 'grace')
  order by s.current_period_start desc nulls last, s.updated_at desc nulls last, s.created_at desc nulls last
  limit 1;

  v_plan_code := lower(coalesce(nullif(v_sub_plan_code, ''), v_ent_plan_code, 'free'));
  v_tier_code := lower(coalesce(v_ent_tier_code, 'free'));

  -- Non-subscription/free path: keep existing free lot behavior untouched.
  if v_sub_id is null or v_plan_code = 'free' or v_ent_included_total <= 0 then
    select
      coalesce(sum(case when l.bucket_type = 'included' and l.status = 'active' then l.granted_amount else 0 end), 0),
      coalesce(sum(case when l.bucket_type = 'included' and l.status = 'active' then l.remaining_amount else 0 end), 0),
      coalesce(sum(case when l.bucket_type = 'included' and l.status = 'active' then l.reserved_amount else 0 end), 0)
    into v_granted, v_remaining, v_reserved
    from public.pricing_credit_lots l
    where l.user_id = p_user_id;

    update public.billing_entitlements
    set included_credits_remaining = v_remaining
    where id = v_ent_id;

    return jsonb_build_object(
      'ok', true,
      'action', 'noop_non_subscription',
      'user_id', p_user_id,
      'plan_code', v_plan_code,
      'tier_code', v_tier_code,
      'included_granted', v_granted,
      'included_remaining', v_remaining,
      'included_reserved', v_reserved
    );
  end if;

  v_cycle_key := coalesce(
    to_char(
      (
        case
          when lower(coalesce(v_sub_provider, '')) = 'google_play' and v_period_end is not null
            then v_period_end
          else v_period_start
        end
      ) at time zone 'UTC',
      'YYYY-MM-DD"T"HH24:MI:SS"Z"'
    ),
    to_char(date_trunc('month', now()) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
  );

  v_source_ref := concat(
    'subscription_cycle:',
    coalesce(nullif(v_sub_provider, ''), 'native_iap'),
    ':',
    coalesce(nullif(v_sub_gateway_id, ''), v_sub_id::text),
    ':',
    v_cycle_key
  );

  -- Replace old plan-included lots for the user. Purchased/top-up lots are not touched.
  update public.pricing_credit_lots l
  set
    status = 'expired',
    expires_at = coalesce(l.expires_at, now()),
    updated_at = now(),
    metadata_json = coalesce(l.metadata_json, '{}'::jsonb)
      || jsonb_build_object(
        'expired_by', 'df_sync_subscription_cycle_credits',
        'replacement_source_ref', v_source_ref,
        'replacement_plan_code', v_plan_code,
        'expired_at', now()
      )
  where l.user_id = p_user_id
    and l.bucket_type = 'included'
    and l.source_type = 'plan_grant'
    and l.status = 'active'
    and coalesce(l.source_ref, '') <> v_source_ref;

  get diagnostics v_expired_count = row_count;

  select l.id
  into v_existing_lot_id
  from public.pricing_credit_lots l
  where l.user_id = p_user_id
    and l.bucket_type = 'included'
    and l.source_type = 'plan_grant'
    and l.source_ref = v_source_ref
    
   limit 1
   for update;

  if v_existing_lot_id is null then
    insert into public.pricing_credit_lots (
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
    values (
      gen_random_uuid(),
      null,
      p_user_id,
      'included',
      'plan_grant',
      v_source_ref,
      v_plan_code,
      v_ent_included_total,
      v_ent_included_total,
      0,
      now(),
      v_period_end,
      'active',
      jsonb_build_object(
        'source', 'df_sync_subscription_cycle_credits',
        'gateway_provider', v_sub_provider,
        'gateway_subscription_id', v_sub_gateway_id,
        'payment_plan_subscription_id', v_sub_id,
        'cycle_key', v_cycle_key,
        'plan_code', v_plan_code,
        'tier_code', v_tier_code,
        'included_credits_total', v_ent_included_total
      ),
      now(),
      now()
    )
     returning id into v_inserted_lot_id;
  else
    update public.pricing_credit_lots
    set
      status = 'active',
      expires_at = v_period_end,
      plan_code_at_grant = v_plan_code,
      granted_amount = greatest(coalesce(granted_amount, 0), v_ent_included_total),
      remaining_amount = case
        when status = 'expired'
          then v_ent_included_total
        else least(
          greatest(coalesce(remaining_amount, 0), 0),
          greatest(coalesce(granted_amount, 0), v_ent_included_total)
        )
      end,
      reserved_amount = least(coalesce(reserved_amount, 0), v_ent_included_total),
      metadata_json = case
        when jsonb_typeof(coalesce(metadata_json, '{}'::jsonb)) = 'object'
          then coalesce(metadata_json, '{}'::jsonb)
        else '{}'::jsonb
      end || jsonb_build_object(
        'source', 'df_sync_subscription_cycle_credits',
        'repair_reason', 'existing_subscription_cycle_lot_reused_without_on_conflict',
        'gateway_provider', v_sub_provider,
        'gateway_subscription_id', v_sub_gateway_id,
        'payment_plan_subscription_id', v_sub_id,
        'cycle_key', v_cycle_key,
        'plan_code', v_plan_code,
        'tier_code', v_tier_code,
        'included_credits_total', v_ent_included_total,
        'repaired_at', now()
      ),
      updated_at = now()
    where id = v_existing_lot_id
    returning id into v_inserted_lot_id;
  end if;

  select
    coalesce(sum(case when l.bucket_type = 'included' and l.status = 'active' then l.granted_amount else 0 end), 0),
    coalesce(sum(case when l.bucket_type = 'included' and l.status = 'active' then l.remaining_amount else 0 end), 0),
    coalesce(sum(case when l.bucket_type = 'included' and l.status = 'active' then l.reserved_amount else 0 end), 0)
  into v_granted, v_remaining, v_reserved
  from public.pricing_credit_lots l
  where l.user_id = p_user_id;

  update public.billing_entitlements
  set
    included_credits_total = v_ent_included_total,
    included_credits_remaining = v_remaining,
    plan_code = v_plan_code,
    tier_code = v_tier_code,
    billing_mode = case when v_tier_code = 'free' then 'free' else 'subscription' end,
    settlement_mode = coalesce(nullif(v_ent_settlement_mode, ''), 'credits'),
    metadata_json = coalesce(metadata_json, '{}'::jsonb)
      || jsonb_build_object(
        'last_subscription_cycle_sync_at', now(),
        'last_subscription_cycle_source_ref', v_source_ref,
        'last_subscription_cycle_lot_id', v_inserted_lot_id,
        'last_subscription_cycle_plan_code', v_plan_code
      )
  where id = v_ent_id;

  return jsonb_build_object(
    'ok', true,
    'action', case when v_existing_lot_id is null then 'created_subscription_cycle_lot' else 'existing_subscription_cycle_lot' end,
    'user_id', p_user_id,
    'plan_code', v_plan_code,
    'tier_code', v_tier_code,
    'subscription_id', v_sub_id,
    'source_ref', v_source_ref,
    'lot_id', v_inserted_lot_id,
    'expired_previous_included_lots', v_expired_count,
    'included_granted', v_granted,
    'included_remaining', v_remaining,
    'included_reserved', v_reserved
  );
end;
$function$


