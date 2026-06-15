begin;

create or replace view public.v_pricing_account_overview as
with active_entitlement as (
    select distinct on (be.user_id)
        be.user_id,
        lower(coalesce(be.tier_code, 'free'::text)) as tier_code,
        coalesce(nullif(lower(be.plan_code), ''::text), lower(coalesce(be.tier_code, 'free'::text))) as plan_code,
        lower(coalesce(
            be.billing_mode,
            case
                when lower(coalesce(be.tier_code, 'free'::text)) = 'free'::text then 'free'::text
                else 'subscription'::text
            end
        )) as billing_mode,
        lower(coalesce(be.settlement_mode, 'credits'::text)) as settlement_mode,
        coalesce(be.included_credits_total, 0::numeric)::numeric(18,4) as included_credits_total,
        coalesce(be.included_credits_remaining, 0::numeric)::numeric(18,4) as entitlement_included_credits_remaining_legacy,
        be.effective_from,
        be.effective_to,
        be.updated_at,
        be.source
    from public.billing_entitlements be
    where (be.effective_from is null or be.effective_from <= now())
      and (be.effective_to is null or be.effective_to > now())
    order by be.user_id, be.effective_from desc nulls last, be.updated_at desc nulls last
), tier_defaults as (
    select
        lower(t.code) as tier_code,
        coalesce(t.monthly_grant_credits, 0::bigint)::numeric(18,4) as monthly_grant_credits
    from public.pricing_tiers t
    where t.is_active = true
), lot_summary as (
    select
        coalesce(l.user_id, bam.user_id) as user_id,
        count(*) filter (where l.bucket_type = 'included'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())) as included_lot_count,
        count(*) filter (where l.bucket_type = 'purchased'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())) as purchased_lot_count,
        count(*) filter (where l.bucket_type = 'promo'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())) as promo_lot_count,
        count(*) filter (where l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())) as active_lot_count,
        sum(
            case
                when l.bucket_type = 'included'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then greatest(coalesce(l.remaining_amount, 0::numeric) - coalesce(l.reserved_amount, 0::numeric), 0::numeric)
                else 0::numeric
            end
        ) as included_available,
        sum(
            case
                when l.bucket_type = 'included'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then coalesce(l.reserved_amount, 0::numeric)
                else 0::numeric
            end
        ) as included_reserved,
        sum(
            case
                when l.bucket_type = 'included'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then coalesce(l.remaining_amount, 0::numeric)
                else 0::numeric
            end
        ) as included_balance,
        sum(
            case
                when l.bucket_type = 'purchased'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then greatest(coalesce(l.remaining_amount, 0::numeric) - coalesce(l.reserved_amount, 0::numeric), 0::numeric)
                else 0::numeric
            end
        ) as purchased_available,
        sum(
            case
                when l.bucket_type = 'purchased'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then coalesce(l.reserved_amount, 0::numeric)
                else 0::numeric
            end
        ) as purchased_reserved,
        sum(
            case
                when l.bucket_type = 'purchased'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then coalesce(l.remaining_amount, 0::numeric)
                else 0::numeric
            end
        ) as purchased_balance,
        sum(
            case
                when l.bucket_type = 'promo'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then greatest(coalesce(l.remaining_amount, 0::numeric) - coalesce(l.reserved_amount, 0::numeric), 0::numeric)
                else 0::numeric
            end
        ) as promo_available,
        sum(
            case
                when l.bucket_type = 'promo'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then coalesce(l.reserved_amount, 0::numeric)
                else 0::numeric
            end
        ) as promo_reserved,
        sum(
            case
                when l.bucket_type = 'promo'::text and l.status = 'active'::text and (l.expires_at is null or l.expires_at > now())
                then coalesce(l.remaining_amount, 0::numeric)
                else 0::numeric
            end
        ) as promo_balance
    from public.pricing_credit_lots l
    left join public.pricing_billing_account_members bam
      on bam.billing_account_id = l.billing_account_id
    group by coalesce(l.user_id, bam.user_id)
), account_summary as (
    select
        pca.user_id,
        pca.balance_credits,
        pca.reserved_credits,
        pca.billing_account_id,
        pca.settlement_mode as account_settlement_mode,
        pca.updated_at as account_updated_at
    from public.pricing_credit_accounts pca
), resolved as (
    select
        coalesce(ae.user_id, ls.user_id, ac.user_id) as user_id,
        ae.tier_code,
        ae.plan_code,
        ae.billing_mode,
        ae.settlement_mode,
        ae.included_credits_total,
        ae.entitlement_included_credits_remaining_legacy,
        ae.effective_from,
        ae.updated_at,
        ae.source,
        td.monthly_grant_credits,
        coalesce(ls.included_lot_count, 0)::bigint as included_lot_count,
        coalesce(ls.purchased_lot_count, 0)::bigint as purchased_lot_count,
        coalesce(ls.promo_lot_count, 0)::bigint as promo_lot_count,
        coalesce(ls.active_lot_count, 0)::bigint as active_lot_count,
        ls.included_available,
        ls.included_reserved,
        ls.included_balance,
        ls.purchased_available,
        ls.purchased_reserved,
        ls.purchased_balance,
        ls.promo_available,
        ls.promo_reserved,
        ls.promo_balance,
        ac.balance_credits,
        ac.reserved_credits,
        ac.billing_account_id,
        ac.account_settlement_mode,
        ac.account_updated_at
    from active_entitlement ae
    full join lot_summary ls
      on ls.user_id = ae.user_id
    full join account_summary ac
      on ac.user_id = coalesce(ae.user_id, ls.user_id)
    left join tier_defaults td
      on td.tier_code = coalesce(ae.tier_code, 'free'::text)
), normalized as (
    select
        r.user_id,
        coalesce(r.tier_code, 'free'::text) as tier_code,
        coalesce(nullif(r.plan_code, ''::text), coalesce(r.tier_code, 'free'::text)) as plan_code,
        coalesce(
            nullif(r.billing_mode, ''::text),
            case when coalesce(r.tier_code, 'free'::text) = 'free'::text then 'free'::text else 'subscription'::text end
        ) as billing_mode,
        coalesce(nullif(r.settlement_mode, ''::text), 'credits'::text) as settlement_mode,
        case
            when coalesce(r.included_credits_total, 0::numeric) > 0::numeric then r.included_credits_total
            when coalesce(r.monthly_grant_credits, 0::numeric) > 0::numeric then r.monthly_grant_credits
            else 0::numeric
        end::numeric(18,4) as included_credits_total,
        coalesce(r.entitlement_included_credits_remaining_legacy, 0::numeric)::numeric(18,4) as entitlement_included_credits_remaining_legacy,
        r.effective_from,
        r.updated_at,
        r.source,
        r.included_lot_count,
        r.purchased_lot_count,
        r.promo_lot_count,
        r.active_lot_count,
        case
            when coalesce(r.included_lot_count, 0) > 0 then coalesce(r.included_available, 0::numeric)
            when coalesce(r.active_lot_count, 0) = 0 then greatest(coalesce(r.balance_credits, 0::bigint)::numeric - coalesce(r.reserved_credits, 0::bigint)::numeric, 0::numeric)
            else 0::numeric
        end::numeric(18,4) as included_available_live,
        case
            when coalesce(r.included_lot_count, 0) > 0 then coalesce(r.included_reserved, 0::numeric)
            when coalesce(r.active_lot_count, 0) = 0 then coalesce(r.reserved_credits, 0::bigint)::numeric
            else 0::numeric
        end::numeric(18,4) as included_reserved_live,
        case
            when coalesce(r.included_lot_count, 0) > 0 then coalesce(r.included_balance, 0::numeric)
            when coalesce(r.active_lot_count, 0) = 0 then coalesce(r.balance_credits, 0::bigint)::numeric + coalesce(r.reserved_credits, 0::bigint)::numeric
            else 0::numeric
        end::numeric(18,4) as included_balance_live,
        coalesce(r.purchased_available, 0::numeric)::numeric(18,4) as purchased_available_live,
        coalesce(r.purchased_reserved, 0::numeric)::numeric(18,4) as purchased_reserved_live,
        coalesce(r.purchased_balance, 0::numeric)::numeric(18,4) as purchased_balance_live,
        coalesce(r.promo_available, 0::numeric)::numeric(18,4) as promo_available_live,
        coalesce(r.promo_reserved, 0::numeric)::numeric(18,4) as promo_reserved_live,
        coalesce(r.promo_balance, 0::numeric)::numeric(18,4) as promo_balance_live,
        coalesce(r.balance_credits, 0::bigint) as balance_credits,
        coalesce(r.reserved_credits, 0::bigint) as reserved_credits,
        r.billing_account_id,
        r.account_settlement_mode,
        r.account_updated_at
    from resolved r
)
select
    user_id,
    jsonb_build_object(
        'source', coalesce(source, 'none'::text),
        'tier_code', tier_code,
        'plan_code', plan_code,
        'billing_mode', billing_mode,
        'settlement_mode', settlement_mode,
        'billing_model', case
            when billing_mode in ('postpaid'::text, 'invoice'::text) or settlement_mode in ('postpaid'::text, 'money'::text) then 'postpaid'::text
            else 'prepaid'::text
        end,
        'included_credits_total', included_credits_total,
        -- Backward-compatible key, now intentionally mapped to LIVE available included credits.
        -- Do not map this to billing_entitlements.included_credits_remaining.
        'included_credits_remaining', included_available_live,
        'included_credits_available', included_available_live,
        'included_credits_reserved', included_reserved_live,
        'included_credits_balance', included_balance_live,
        'entitlement_included_credits_remaining_legacy', entitlement_included_credits_remaining_legacy,
        'effective_from', effective_from,
        'updated_at', updated_at
    ) as plan_json,
    jsonb_build_object(
        'included_available', included_available_live,
        'included_reserved', included_reserved_live,
        'included_balance', included_balance_live,
        'included_used', case
            when coalesce(included_lot_count, 0) > 0 then greatest(coalesce(included_credits_total, 0::numeric) - coalesce(included_balance_live, 0::numeric), 0::numeric)::numeric(18,4)
            else 0::numeric
        end,
        'included_expired_or_unavailable', case
            when coalesce(included_lot_count, 0) > 0 then 0::numeric
            else greatest(coalesce(included_credits_total, 0::numeric) - coalesce(included_balance_live, 0::numeric) - coalesce(included_reserved_live, 0::numeric), 0::numeric)::numeric(18,4)
        end,
        'included_lot_count', coalesce(included_lot_count, 0),
        'wallet_available', purchased_available_live,
        'wallet_reserved', purchased_reserved_live,
        'wallet_balance', purchased_balance_live,
        'purchased_available', purchased_available_live,
        'purchased_reserved', purchased_reserved_live,
        'purchased_balance', purchased_balance_live,
        'promo_available', promo_available_live,
        'promo_reserved', promo_reserved_live,
        'promo_balance', promo_balance_live,
        'total_available', included_available_live + purchased_available_live + promo_available_live,
        'total_reserved', included_reserved_live + purchased_reserved_live + promo_reserved_live,
        'total_spendable', included_available_live + purchased_available_live + promo_available_live,
        'total_balance', included_balance_live + purchased_balance_live + promo_balance_live,
        'source', case when active_lot_count > 0 then 'pricing_credit_lots'::text else 'pricing_credit_accounts_legacy_fallback'::text end
    ) as lots_json,
    jsonb_build_object(
        'legacy_balance_credits', balance_credits,
        'legacy_reserved_credits', reserved_credits,
        'billing_account_id', billing_account_id,
        'settlement_mode', account_settlement_mode,
        'updated_at', account_updated_at
    ) as legacy_account_json
from normalized;


create or replace view public.v_dashboard_pricing_snapshot as
with overview as (
    select
        o.user_id,
        o.plan_json,
        o.lots_json,
        o.legacy_account_json
    from public.v_pricing_account_overview o
), normalized as (
    select
        user_id,
        coalesce(plan_json ->> 'source', 'none') as source,
        coalesce(plan_json ->> 'tier_code', 'free') as tier_code,
        coalesce(nullif(plan_json ->> 'plan_code', ''), 'free') as plan_code,
        coalesce(nullif(plan_json ->> 'billing_mode', ''), 'free') as billing_mode,
        coalesce(nullif(plan_json ->> 'settlement_mode', ''), 'credits') as settlement_mode,
        coalesce(nullif(plan_json ->> 'billing_model', ''), 'prepaid') as billing_model,
        coalesce((plan_json ->> 'included_credits_total')::numeric, 0::numeric)::numeric(18,4) as included_credits_total,
        coalesce((lots_json ->> 'included_available')::numeric, 0::numeric)::numeric(18,4) as included_available,
        coalesce((lots_json ->> 'included_reserved')::numeric, 0::numeric)::numeric(18,4) as included_reserved,
        coalesce(nullif(lots_json ->> 'included_used', '')::numeric, 0::numeric)::numeric(18,4) as included_used,
        coalesce(nullif(lots_json ->> 'included_expired_or_unavailable', '')::numeric, 0::numeric)::numeric(18,4) as included_expired_or_unavailable,
        coalesce((lots_json ->> 'wallet_available')::numeric, (lots_json ->> 'purchased_available')::numeric, 0::numeric)::numeric(18,4) as wallet_available,
        coalesce((lots_json ->> 'wallet_reserved')::numeric, (lots_json ->> 'purchased_reserved')::numeric, 0::numeric)::numeric(18,4) as wallet_reserved,
        coalesce((lots_json ->> 'promo_available')::numeric, 0::numeric)::numeric(18,4) as promo_available,
        coalesce((lots_json ->> 'promo_reserved')::numeric, 0::numeric)::numeric(18,4) as promo_reserved,
        coalesce((lots_json ->> 'total_available')::numeric, (lots_json ->> 'total_spendable')::numeric, 0::numeric)::numeric(18,4) as total_available,
        coalesce((lots_json ->> 'total_reserved')::numeric, 0::numeric)::numeric(18,4) as total_reserved,
        coalesce((lots_json ->> 'total_spendable')::numeric, (lots_json ->> 'total_available')::numeric, 0::numeric)::numeric(18,4) as total_spendable,
        coalesce(plan_json ->> 'updated_at', legacy_account_json ->> 'updated_at') as updated_at_text,
        coalesce(lots_json ->> 'source', 'pricing_account_overview') as credit_source
    from overview
), computed as (
    select
        n.*,
        case
            when n.included_credits_total > 0::numeric then round(coalesce(n.included_used, 0::numeric) / n.included_credits_total * 100::numeric, 2)
            else 0::numeric
        end as usage_percent
    from normalized n
)
select
    user_id,
    jsonb_build_object(
        'source', source,
        'plan_name', case
            when plan_code = 'free'::text then 'Free'::text
            when plan_code = 'pro_monthly_v1'::text then 'Pro'::text
            when plan_code = 'pro_yearly_v1'::text then 'Pro Yearly'::text
            when plan_code = 'business_monthly_v1'::text then 'Business'::text
            when plan_code = 'business_yearly_v1'::text then 'Business Yearly'::text
            when plan_code = 'enterprise_monthly_v1'::text then 'Enterprise'::text
            when plan_code = 'enterprise_yearly_v1'::text then 'Enterprise Yearly'::text
            when plan_code = 'enterprise_contract_v1'::text then 'Enterprise'::text
            else initcap(replace(plan_code, '_'::text, ' '::text))
        end,
        'tier_code', tier_code,
        'plan_code', plan_code,
        'billing_mode', billing_mode,
        'settlement_mode', settlement_mode,
        'billing_model', billing_model,
        'credit_cap', included_credits_total,
        'included_credits_total', included_credits_total
    ) as plan_summary_json,
    jsonb_build_object(
        'source', credit_source,
        'updated_at', updated_at_text,
        'billing_model', billing_model,
        'available_credits', total_available,
        'reserved_credits', total_reserved,
        'total_available', total_available,
        'total_reserved', total_reserved,
        'total_spendable', total_spendable,
        'included_credits_total', included_credits_total,
        -- Backward-compatible key, now intentionally mapped to LIVE included available credits.
        'included_credits_remaining', included_available,
        'included_available', included_available,
        'included_reserved', included_reserved,
        'included_used', included_used,
        'included_expired_or_unavailable', included_expired_or_unavailable,
        'wallet_available', wallet_available,
        'wallet_reserved', wallet_reserved,
        'purchased_available', wallet_available,
        'purchased_reserved', wallet_reserved,
        'promo_available', promo_available,
        'promo_reserved', promo_reserved,
        'usage_percent', usage_percent
    ) as pricing_summary_json,
    jsonb_build_object(
        'source', credit_source,
        'used_credits', included_used,
        'included_used', included_used,
        'included_expired_or_unavailable', included_expired_or_unavailable,
        'usage_percent', usage_percent,
        'billing_model', billing_model
    ) as usage_summary_json,
    jsonb_build_object(
        'used_credits', included_used,
        'included_used', included_used,
        'usage_percent', usage_percent,
        'reserved_credits', total_reserved,
        'available_credits', total_available,
        'total_available', total_available,
        'total_reserved', total_reserved,
        'included_available', included_available,
        'included_reserved', included_reserved,
        'wallet_available', wallet_available,
        'wallet_reserved', wallet_reserved,
        'billing_model', billing_model
    ) as usage_json
from computed;


create or replace view public.v_dashboard_home as
 SELECT user_id,
    updated_at,
    gauges_json,
    alerts_json,
    face_carousel_json,
    video_carousel_json,
    header_json
   FROM dashboard_home_cache;;


commit;
