#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"

for c in "$DB_CONTAINER" "$PRICING_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c"; exit 2; }
done

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — MULTI-PERSON COMMERCIAL GATE"
echo " READ ONLY"
echo "============================================================"
echo "required_multi_person_premium_pct=20"
echo "db_mutation=NONE"
echo "production=UNTOUCHED"

echo
echo "===== ACTIVE FACE VARIANTS / LEAF SKUS ====="
"${PSQL[@]}" -P pager=off -c "
select
  v.code as variant_code,
  v.name,
  v.category,
  v.is_active,
  vl.sku_code,
  vl.qty_mode,
  vl.qty_value,
  vl.qty_param,
  s.unit,
  s.provider_hint,
  s.default_unit_credits,
  s.status,
  s.metadata_json as sku_metadata
from public.pricing_variants v
left join public.pricing_variant_lines vl on vl.variant_code=v.code
left join public.pricing_skus s on s.code=vl.sku_code
where v.code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
order by v.code,vl.sku_code;
"

echo
echo "===== ACTIVE FACE PRICEBOOK OVERRIDES ====="
"${PSQL[@]}" -P pager=off -c "
select
  pb.name,
  pb.country_code,
  pb.currency,
  pb.channel,
  pb.tier_code,
  sp.sku_code,
  sp.unit_credits_override,
  sp.unit_money_override,
  sp.min_qty,
  sp.max_qty,
  sp.metadata_json
from public.pricing_sku_prices sp
join public.pricing_pricebooks pb on pb.id=sp.pricebook_id
where pb.is_active=true
  and sp.sku_code in (
    select distinct vl.sku_code
    from public.pricing_variant_lines vl
    where vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
  )
order by sp.sku_code,pb.currency,pb.channel,pb.tier_code nulls first,pb.name;
"

echo
echo "===== FACE COGS ====="
"${PSQL[@]}" -P pager=off -c "
select
  c.sku_code,
  c.component_code,
  c.cost_model,
  c.cost_currency,
  c.variable_cost_money,
  c.fixed_monthly_cost_money,
  c.assumed_monthly_units,
  c.is_active,
  c.effective_from,
  c.effective_to,
  c.metadata_json
from public.pricing_sku_costs c
where c.sku_code in (
  select distinct vl.sku_code
  from public.pricing_variant_lines vl
  where vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
)
and c.is_active=true
and c.effective_from <= now()
and (c.effective_to is null or c.effective_to > now())
order by c.sku_code,c.component_code;
"

echo
echo "===== ACTIVE VIDEO VARIANTS / LEAF SKUS ====="
"${PSQL[@]}" -P pager=off -c "
select
  v.code as variant_code,
  v.name,
  v.category,
  v.is_active,
  vl.sku_code,
  vl.qty_mode,
  vl.qty_value,
  vl.qty_param,
  s.unit,
  s.provider_hint,
  s.default_unit_credits,
  s.status,
  s.metadata_json as sku_metadata
from public.pricing_variants v
left join public.pricing_variant_lines vl on vl.variant_code=v.code
left join public.pricing_skus s on s.code=vl.sku_code
where v.code like 'TALKING_VIDEO%'
   or v.code='FUSION_TALKING_VIDEO'
order by v.code,vl.sku_code;
"

echo
echo "===== ACTIVE VIDEO PRICEBOOK OVERRIDES ====="
"${PSQL[@]}" -P pager=off -c "
select
  pb.name,
  pb.country_code,
  pb.currency,
  pb.channel,
  pb.tier_code,
  sp.sku_code,
  sp.unit_credits_override,
  sp.unit_money_override,
  sp.min_qty,
  sp.max_qty,
  sp.metadata_json
from public.pricing_sku_prices sp
join public.pricing_pricebooks pb on pb.id=sp.pricebook_id
where pb.is_active=true
  and (
    sp.sku_code like 'LONGFORM_TALK%'
    or sp.sku_code='FUSION_TALK_MIN'
  )
order by sp.sku_code,pb.currency,pb.channel,pb.tier_code nulls first,pb.name;
"

echo
echo "===== VIDEO COGS ====="
"${PSQL[@]}" -P pager=off -c "
select
  c.sku_code,
  c.component_code,
  c.cost_model,
  c.cost_currency,
  c.variable_cost_money,
  c.fixed_monthly_cost_money,
  c.assumed_monthly_units,
  c.is_active,
  c.effective_from,
  c.effective_to,
  c.metadata_json
from public.pricing_sku_costs c
where (
  c.sku_code like 'LONGFORM_TALK%'
  or c.sku_code='FUSION_TALK_MIN'
)
and c.is_active=true
and c.effective_from <= now()
and (c.effective_to is null or c.effective_to > now())
order by c.sku_code,c.component_code;
"

echo
echo "===== RECENT FACE QUOTE ECONOMICS ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  status,
  quote_json->>'service_action' as service_action,
  quote_json->>'variant_code' as variant_code,
  quote_json->>'sku_code' as requested_sku,
  quote_json->'lines' as lines,
  quote_json->'economics' as economics,
  quote_json->'economics_final' as economics_final
from public.pricing_credit_reservations
where quote_json->>'service_action' in (
  'face.creator.generate.t2i',
  'face.creator.generate.i2i'
)
   or quote_json::text ~* 'FACE_MULTI_PERSON'
order by created_at desc
limit 20;
"

echo
echo "===== RECENT VIDEO QUOTE ECONOMICS ====="
"${PSQL[@]}" -P pager=off -c "
select
  created_at,
  status,
  quote_json->>'service_action' as service_action,
  quote_json->>'variant_code' as variant_code,
  quote_json->>'sku_code' as requested_sku,
  quote_json->'lines' as lines,
  quote_json->'economics' as economics,
  quote_json->'economics_final' as economics_final,
  quote_json->'meta'->>'conversation_mode' as conversation_mode,
  quote_json->'meta'->>'pricing_strategy' as pricing_strategy
from public.pricing_credit_reservations
where quote_json::text ~* 'talking_video|longform_talk|shared_scene|ordered_speaker_shots'
order by created_at desc
limit 25;
"

echo
echo "===== COMMERCIAL RULE CHECKS ====="
"${PSQL[@]}" -P pager=off -c "
with leaf as (
  select
    vl.variant_code,
    vl.sku_code,
    coalesce(sp.unit_credits_override,s.default_unit_credits)::numeric as credits,
    pb.currency,
    pb.channel,
    pb.tier_code,
    pb.country_code,
    pb.name as pricebook_name
  from public.pricing_variant_lines vl
  join public.pricing_skus s on s.code=vl.sku_code
  join public.pricing_sku_prices sp on sp.sku_code=s.code
  join public.pricing_pricebooks pb on pb.id=sp.pricebook_id
  where pb.is_active=true
    and vl.variant_code in ('FACE_T2I','FACE_I2I','FACE_MULTI_PERSON')
),
face_compare as (
  select
    b.pricebook_name,
    b.currency,
    b.channel,
    b.tier_code,
    b.country_code,
    b.variant_code as base_variant,
    b.credits as base_credits,
    m.credits as multi_credits,
    round((m.credits/nullif(b.credits,0)-1)*100,2) as uplift_pct,
    case
      when m.credits >= b.credits*1.20 then 'PASS'
      else 'FAIL'
    end as minimum_20pct_rule
  from leaf b
  join leaf m
    on m.pricebook_name=b.pricebook_name
   and m.currency=b.currency
   and m.channel=b.channel
   and coalesce(m.tier_code,'')=coalesce(b.tier_code,'')
   and m.variant_code='FACE_MULTI_PERSON'
  where b.variant_code in ('FACE_T2I','FACE_I2I')
)
select * from face_compare
order by currency,channel,tier_code nulls first,base_variant;
"

echo
echo "===== COGS COMPLETENESS CHECK ====="
"${PSQL[@]}" -P pager=off -c "
with required(sku_code,provider_pattern) as (
  values
    (
      coalesce((
        select vl.sku_code
        from public.pricing_variant_lines vl
        where vl.variant_code='FACE_MULTI_PERSON'
        limit 1
      ),'FACE_MULTI_PERSON'),
      'openai|gpt.?image'
    ),
    ('LONGFORM_TALK_PREMIUM_SECOND','sync|sync3')
)
select
  r.sku_code,
  case when exists (
    select 1
    from public.pricing_sku_costs c
    where c.sku_code=r.sku_code
      and c.is_active=true
      and c.effective_from <= now()
      and (c.effective_to is null or c.effective_to > now())
  ) then 'PASS' else 'FAIL' end as active_cost_row,
  case when exists (
    select 1
    from public.pricing_sku_costs c
    where c.sku_code=r.sku_code
      and c.is_active=true
      and c.effective_from <= now()
      and (c.effective_to is null or c.effective_to > now())
      and (
        c.component_code ~* r.provider_pattern
        or coalesce(c.metadata_json::text,'') ~* r.provider_pattern
      )
  ) then 'PASS' else 'FAIL' end as provider_alignment
from required r;
"

echo
echo "============================================================"
echo "NEXT_DECISION:"
echo "1. Do not change customer prices until the live comparison above is reviewed."
echo "2. COGS rows must reflect current providers, not legacy Fal/HeyGen assumptions."
echo "3. Multi-person customer price must be >= equivalent single-person price * 1.20."
echo "4. If video has no distinct multi-person SKU, create one rather than overloading the single-person SKU."
echo "5. After pricing changes, require fresh preview/reserve/commit proof."
echo "db_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
