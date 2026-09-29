#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: run on desifaces-dev"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"
FACE_CONTAINER="${FACE_CONTAINER:-df-svc-face}"
FUSION_CONTAINER="${FUSION_CONTAINER:-df-svc-fusion}"
FUSION_WORKER_CONTAINER="${FUSION_WORKER_CONTAINER:-df-svc-fusion-worker}"

for c in "$DB_CONTAINER" "$PRICING_CONTAINER" "$FACE_CONTAINER" "$FUSION_CONTAINER" "$FUSION_WORKER_CONTAINER"; do
  docker inspect "$c" >/dev/null 2>&1 || { echo "FAIL: missing container $c"; exit 2; }
done

FACE_SKU="$(docker exec "$FACE_CONTAINER" sh -lc 'printf "%s" "${DF_PRICING_SKU_FACE_T2I:-face.creator.generate.t2i}"')"
FACE_MODEL="$(docker exec "$FACE_CONTAINER" sh -lc 'printf "%s" "${OPENAI_IMAGE_MODEL_T2I:-gpt-image-2}"')"
FACE_QUALITY="$(docker exec "$FACE_CONTAINER" sh -lc 'printf "%s" "${OPENAI_IMAGE_QUALITY:-high}"')"
FACE_SIZE="$(docker exec "$FACE_CONTAINER" sh -lc 'printf "%s" "${OPENAI_IMAGE_SIZE:-auto}"')"
SYNC_MODEL="$(docker exec "$FUSION_WORKER_CONTAINER" sh -lc 'printf "%s" "${DF_SYNC3_MODEL_ID:-sync-3}"')"
SYNC_CAPACITY="$(docker exec "$FUSION_WORKER_CONTAINER" sh -lc 'printf "%s" "${DF_SYNC3_PROVIDER_CONCURRENCY:-unset}"')"

DATABASE_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
[[ -n "$DATABASE_URL" ]] || { echo "FAIL: pricing DATABASE_URL unavailable"; exit 2; }

DB_USER="$(printf '%s' "$DATABASE_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DATABASE_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"

[[ -n "$DB_USER" && "$DB_USER" != "$DATABASE_URL" ]] || { echo "FAIL: could not resolve live DB user"; exit 2; }
[[ -n "$DB_NAME" && "$DB_NAME" != "$DATABASE_URL" ]] || { echo "FAIL: could not resolve live DB name"; exit 2; }

PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

sql_scalar() {
  "${PSQL[@]}" -Atqc "$1"
}

echo "============================================================"
echo " desifaces DEV — GROUP PHOTO / VIDEO PRICING CERTIFICATION"
echo " READ ONLY"
echo "============================================================"
echo "production=UNTOUCHED"
echo "db_mutation=NONE"
echo
echo "===== RUNTIME PROVIDERS ====="
echo "face_pricing_variant=$FACE_SKU"
echo "face_provider=openai"
echo "face_model=$FACE_MODEL"
echo "face_quality=$FACE_QUALITY"
echo "face_size=$FACE_SIZE"
echo "video_provider=sync"
echo "video_model=$SYNC_MODEL"
echo "sync_provider_capacity=$SYNC_CAPACITY"
echo "db_user=$DB_USER"
echo "db_name=$DB_NAME"

FACE_VARIANT_COUNT="$(sql_scalar "select count(*) from public.pricing_variants where code='${FACE_SKU//\'/\'\'}' and is_active=true;")"
FACE_LINE_COUNT="$(sql_scalar "select count(*) from public.pricing_variant_lines where variant_code='${FACE_SKU//\'/\'\'}';")"
FACE_BAD_LEAF_COUNT="$(sql_scalar "
select count(*)
from public.pricing_variant_lines vl
left join public.pricing_skus s on s.code=vl.sku_code
where vl.variant_code='${FACE_SKU//\'/\'\'}'
  and (s.code is null or s.status <> 'active');
")"
FACE_COST_MISSING="$(sql_scalar "
select count(*)
from (
  select distinct vl.sku_code
  from public.pricing_variant_lines vl
  where vl.variant_code='${FACE_SKU//\'/\'\'}'
) x
where not exists (
  select 1 from public.pricing_sku_costs c
  where c.sku_code=x.sku_code
    and c.is_active=true
    and c.effective_from <= now()
    and (c.effective_to is null or c.effective_to > now())
);
")"
FACE_PROVIDER_COST_MATCH="$(sql_scalar "
select count(*)
from public.pricing_variant_lines vl
join public.pricing_sku_costs c on c.sku_code=vl.sku_code
where vl.variant_code='${FACE_SKU//\'/\'\'}'
  and c.is_active=true
  and c.effective_from <= now()
  and (c.effective_to is null or c.effective_to > now())
  and (
    c.component_code ~* 'openai|gpt.?image'
    or coalesce(c.metadata_json::text,'') ~* 'openai|gpt.?image'
  );
")"

VIDEO_VARIANT_COUNT="$(sql_scalar "select count(*) from public.pricing_variants where code='TALKING_VIDEO_PREMIUM_SECOND' and is_active=true;")"
VIDEO_LINE_OK="$(sql_scalar "
select count(*)
from public.pricing_variant_lines
where variant_code='TALKING_VIDEO_PREMIUM_SECOND'
  and sku_code='LONGFORM_TALK_PREMIUM_SECOND'
  and qty_mode='param'
  and qty_param='requested_units';
")"
VIDEO_SKU_OK="$(sql_scalar "
select count(*)
from public.pricing_skus
where code='LONGFORM_TALK_PREMIUM_SECOND'
  and status='active'
  and unit='second'
  and default_unit_credits=15;
")"
VIDEO_BAD_PRICEBOOK="$(sql_scalar "
select count(*)
from public.pricing_sku_prices sp
join public.pricing_pricebooks pb on pb.id=sp.pricebook_id
where sp.sku_code='LONGFORM_TALK_PREMIUM_SECOND'
  and pb.is_active=true
  and pb.channel in ('web','mobile')
  and (
    sp.unit_credits_override is distinct from 15
    or sp.unit_money_override is not null
    or coalesce(sp.min_qty,0) <> 10
  );
")"
VIDEO_PRICEBOOK_COUNT="$(sql_scalar "
select count(*)
from public.pricing_sku_prices sp
join public.pricing_pricebooks pb on pb.id=sp.pricebook_id
where sp.sku_code='LONGFORM_TALK_PREMIUM_SECOND'
  and pb.is_active=true
  and pb.channel in ('web','mobile');
")"
VIDEO_COST_MISSING="$(sql_scalar "
select case when exists (
  select 1 from public.pricing_sku_costs c
  where c.sku_code='LONGFORM_TALK_PREMIUM_SECOND'
    and c.is_active=true
    and c.effective_from <= now()
    and (c.effective_to is null or c.effective_to > now())
) then 0 else 1 end;
")"
VIDEO_PROVIDER_COST_MATCH="$(sql_scalar "
select count(*)
from public.pricing_sku_costs c
where c.sku_code='LONGFORM_TALK_PREMIUM_SECOND'
  and c.is_active=true
  and c.effective_from <= now()
  and (c.effective_to is null or c.effective_to > now())
  and (
    c.component_code ~* 'sync|sync3'
    or coalesce(c.metadata_json::text,'') ~* 'sync|sync3'
  );
")"

echo
echo "===== GROUP PHOTO SKU ====="
"${PSQL[@]}" -P pager=off -c "
select v.code as variant_code,v.name,v.category,v.is_active,v.metadata_json
from public.pricing_variants v
where v.code='${FACE_SKU//\'/\'\'}';

select vl.variant_code,vl.sku_code,vl.qty_mode,vl.qty_value,vl.qty_param,
       s.name as leaf_name,s.unit,s.provider_hint,s.default_unit_credits,s.status,
       s.metadata_json as sku_metadata
from public.pricing_variant_lines vl
left join public.pricing_skus s on s.code=vl.sku_code
where vl.variant_code='${FACE_SKU//\'/\'\'}'
order by vl.sku_code;
"

echo "===== GROUP PHOTO COGS ====="
"${PSQL[@]}" -P pager=off -c "
select c.sku_code,c.component_code,c.cost_model,c.cost_currency,
       c.variable_cost_money,c.fixed_monthly_cost_money,c.assumed_monthly_units,
       c.is_active,c.effective_from,c.effective_to,c.metadata_json
from public.pricing_sku_costs c
where c.sku_code in (
  select vl.sku_code from public.pricing_variant_lines vl
  where vl.variant_code='${FACE_SKU//\'/\'\'}'
)
  and c.is_active=true
  and c.effective_from <= now()
  and (c.effective_to is null or c.effective_to > now())
order by c.sku_code,c.component_code;
"

echo "===== VIDEO SKU ====="
"${PSQL[@]}" -P pager=off -c "
select v.code as variant_code,v.name,v.category,v.is_active,v.metadata_json
from public.pricing_variants v
where v.code='TALKING_VIDEO_PREMIUM_SECOND';

select vl.variant_code,vl.sku_code,vl.qty_mode,vl.qty_value,vl.qty_param,
       s.name as leaf_name,s.unit,s.provider_hint,s.default_unit_credits,s.status,
       s.metadata_json as sku_metadata
from public.pricing_variant_lines vl
join public.pricing_skus s on s.code=vl.sku_code
where vl.variant_code='TALKING_VIDEO_PREMIUM_SECOND';

select pb.name,pb.country_code,pb.currency,pb.channel,pb.tier_code,
       sp.unit_credits_override,sp.unit_money_override,sp.min_qty,sp.max_qty,
       sp.metadata_json
from public.pricing_sku_prices sp
join public.pricing_pricebooks pb on pb.id=sp.pricebook_id
where sp.sku_code='LONGFORM_TALK_PREMIUM_SECOND'
  and pb.is_active=true
  and pb.channel in ('web','mobile')
order by pb.currency,pb.channel,pb.tier_code nulls first,pb.name;
"

echo "===== VIDEO COGS ====="
"${PSQL[@]}" -P pager=off -c "
select c.sku_code,c.component_code,c.cost_model,c.cost_currency,
       c.variable_cost_money,c.fixed_monthly_cost_money,c.assumed_monthly_units,
       c.is_active,c.effective_from,c.effective_to,c.metadata_json
from public.pricing_sku_costs c
where c.sku_code='LONGFORM_TALK_PREMIUM_SECOND'
  and c.is_active=true
  and c.effective_from <= now()
  and (c.effective_to is null or c.effective_to > now())
order by c.component_code;
"

echo "===== LATEST LIVE QUOTE ECONOMICS ====="
"${PSQL[@]}" -P pager=off -c "
select created_at,status,
       quote_json->>'service_name' as service_name,
       quote_json->>'service_action' as service_action,
       quote_json->>'sku_code' as requested_variant,
       quote_json->>'variant_code' as variant_code,
       quote_json->'lines' as lines,
       quote_json->'economics' as economics,
       quote_json->'economics_final' as economics_final
from public.pricing_credit_reservations
where quote_json->>'service_action' in (
  'face.creator.generate.t2i',
  'fusion.longform.talking_video_premium_second'
)
order by created_at desc
limit 8;
"

GROUP_SKU_VERDICT=FAIL
GROUP_COGS_VERDICT=FAIL
VIDEO_SKU_VERDICT=FAIL
VIDEO_COGS_VERDICT=FAIL

if [[ "$FACE_VARIANT_COUNT" == "1" && "$FACE_LINE_COUNT" -gt 0 && "$FACE_BAD_LEAF_COUNT" == "0" ]]; then
  GROUP_SKU_VERDICT=PASS
fi
if [[ "$FACE_COST_MISSING" == "0" && "$FACE_PROVIDER_COST_MATCH" -gt 0 ]]; then
  GROUP_COGS_VERDICT=PASS
fi
if [[ "$VIDEO_VARIANT_COUNT" == "1" && "$VIDEO_LINE_OK" == "1" && "$VIDEO_SKU_OK" == "1" && "$VIDEO_PRICEBOOK_COUNT" -gt 0 && "$VIDEO_BAD_PRICEBOOK" == "0" ]]; then
  VIDEO_SKU_VERDICT=PASS
fi
if [[ "$VIDEO_COST_MISSING" == "0" && "$VIDEO_PROVIDER_COST_MATCH" -gt 0 ]]; then
  VIDEO_COGS_VERDICT=PASS
fi

echo
echo "============================================================"
echo " GROUP_PHOTO_SKU=$GROUP_SKU_VERDICT"
echo " GROUP_PHOTO_COGS=$GROUP_COGS_VERDICT"
echo " VIDEO_SKU=$VIDEO_SKU_VERDICT"
echo " VIDEO_COGS=$VIDEO_COGS_VERDICT"
echo " face_variant=$FACE_SKU"
echo " face_model=$FACE_MODEL"
echo " video_variant=TALKING_VIDEO_PREMIUM_SECOND"
echo " video_leaf_sku=LONGFORM_TALK_PREMIUM_SECOND"
echo " db_mutation=NONE"
echo " production=UNTOUCHED"
echo "============================================================"
