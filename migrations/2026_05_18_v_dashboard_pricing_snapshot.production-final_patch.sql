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
        greatest(n.included_credits_total - n.included_available - n.included_reserved, 0::numeric)::numeric(18,4) as included_used,
        case
            when n.included_credits_total > 0::numeric then round(greatest(n.included_credits_total - n.included_available - n.included_reserved, 0::numeric) / n.included_credits_total * 100::numeric, 2)
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
