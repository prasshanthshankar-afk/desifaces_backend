-- Production-safe pricing snapshot view for dashboard/home.
-- This view does not assume a nonexistent charged_credits column.
-- It uses:
--   - pricing_credit_accounts for available/reserved
--   - pricing_credit_reservations.quote_json / reserved_credits for best-effort used credits
--
-- Notes:
-- 1) plan_name is derived from the latest non-null tier_code seen in reservations.
-- 2) used_credits is best-effort:
--    - prefers final/charged credits from quote_json if present
--    - otherwise falls back to reserved_credits for committed/finalized rows
-- 3) usage_percent is computed from used / (available + used) when possible.

create or replace view public.v_dashboard_pricing_snapshot as
with latest_tier as (
    select distinct on (r.user_id)
        r.user_id,
        r.tier_code,
        coalesce(r.finalized_at, r.updated_at, r.created_at) as sort_ts
    from public.pricing_credit_reservations r
    where r.tier_code is not null
    order by r.user_id, coalesce(r.finalized_at, r.updated_at, r.created_at) desc
),
usage_rollup as (
    select
        r.user_id,
        coalesce(
            sum(
                case
                    when lower(coalesce(r.status, '')) in ('committed', 'finalized', 'charged', 'completed', 'invoiced')
                    then coalesce(
                        nullif(r.quote_json ->> 'final_charged_credits', '')::numeric,
                        nullif(r.quote_json -> 'pricing_summary' ->> 'final_charged_credits', '')::numeric,
                        nullif(r.quote_json -> 'pricing' ->> 'final_charged_credits', '')::numeric,
                        nullif(r.quote_json ->> 'charged_credits', '')::numeric,
                        nullif(r.quote_json -> 'pricing_summary' ->> 'charged_credits', '')::numeric,
                        nullif(r.quote_json -> 'pricing' ->> 'charged_credits', '')::numeric,
                        nullif(r.quote_json ->> 'reserved_credits', '')::numeric,
                        r.reserved_credits::numeric,
                        0::numeric
                    )
                    else 0::numeric
                end
            ),
            0::numeric
        ) as used_credits
    from public.pricing_credit_reservations r
    group by r.user_id
)
select
    pca.user_id,
    jsonb_build_object(
        'plan_name',
        case
            when lt.tier_code is null or btrim(lt.tier_code) = '' then 'Free'
            else initcap(replace(lt.tier_code, '_', ' '))
        end,
        'tier_code', lt.tier_code,
        'source', 'pricing_credit_reservations.tier_code'
    ) as plan_summary_json,
    jsonb_build_object(
        'available_credits', coalesce(pca.balance_credits, 0),
        'reserved_credits', coalesce(pca.reserved_credits, 0),
        'updated_at', pca.updated_at,
        'source', 'pricing_credit_accounts'
    ) as pricing_summary_json,
    jsonb_build_object(
        'used_credits', coalesce(ur.used_credits, 0),
        'usage_percent',
            case
                when (coalesce(pca.balance_credits, 0) + coalesce(ur.used_credits, 0)) > 0
                then round(
                    (
                        coalesce(ur.used_credits, 0)
                        / (coalesce(pca.balance_credits, 0) + coalesce(ur.used_credits, 0))
                    ) * 100.0,
                    2
                )
                else null
            end,
        'source', 'pricing_credit_reservations'
    ) as usage_summary_json,
    jsonb_build_object(
        'used_credits', coalesce(ur.used_credits, 0),
        'available_credits', coalesce(pca.balance_credits, 0),
        'reserved_credits', coalesce(pca.reserved_credits, 0),
        'usage_percent',
            case
                when (coalesce(pca.balance_credits, 0) + coalesce(ur.used_credits, 0)) > 0
                then round(
                    (
                        coalesce(ur.used_credits, 0)
                        / (coalesce(pca.balance_credits, 0) + coalesce(ur.used_credits, 0))
                    ) * 100.0,
                    2
                )
                else null
            end
    ) as usage_json
from public.pricing_credit_accounts pca
left join latest_tier lt
  on lt.user_id = pca.user_id
left join usage_rollup ur
  on ur.user_id = pca.user_id;
