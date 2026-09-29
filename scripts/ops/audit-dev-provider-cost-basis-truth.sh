#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || { echo "FAIL: DEV host required"; exit 2; }

DB_CONTAINER="${DB_CONTAINER:-desifaces-db}"
PRICING_CONTAINER="${PRICING_CONTAINER:-df-svc-pricing}"
DB_URL="$(docker exec "$PRICING_CONTAINER" sh -lc 'printf "%s" "$DATABASE_URL"')"
DB_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[a-zA-Z0-9+.-]+://([^:/@]+).*#\1#')"
DB_NAME="$(printf '%s' "$DB_URL" | sed -E 's#^.*/([^/?]+)(\?.*)?$#\1#')"
PSQL=(docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME")

echo "============================================================"
echo " desifaces DEV — PROVIDER / COST BASIS TRUTH"
echo " READ ONLY"
echo "============================================================"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"

echo
echo "===== 1. LIVE RUNTIME PROVIDER SETTINGS (NON-SECRET ONLY) ====="
for c in df-svc-face df-svc-face-worker df-svc-audio df-svc-audio-worker df-svc-fusion df-svc-fusion-worker df-svc-fusion-extension df-svc-fusion-extension-worker; do
  docker inspect "$c" >/dev/null 2>&1 || continue
  echo
  echo "--- $c ---"
  docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' |
    grep -Ei '(^|_)(PROVIDER|MODEL|QUALITY|IMAGE_SIZE|VIDEO_MODE|MOTION_MODE)=' |
    grep -Evi '(KEY|SECRET|TOKEN|PASSWORD|CONNECTION|BEARER|PAT)=' |
    sort || true
done

echo
echo "===== 2. IN-CONTAINER PRICING / PROVIDER SOURCE MARKERS ====="
if docker inspect df-svc-face >/dev/null 2>&1; then
  docker exec df-svc-face sh -lc '
    echo "[svc-face]"
    grep -R -nE "DF_IMAGE_PROVIDER_DEFAULT|OPENAI_IMAGE_MODEL_T2I|OPENAI_IMAGE_MODEL_EDIT|OPENAI_IMAGE_QUALITY|model_t2i|model_edit"       /app/app/services/providers /app/app/config.py 2>/dev/null | head -n 80 || true
  '
fi
if docker inspect df-svc-fusion >/dev/null 2>&1; then
  docker exec df-svc-fusion sh -lc '
    echo "[svc-fusion]"
    grep -R -nE "FUSION_MULTI_PERSON|FUSION_TALKING_VIDEO|FUSION_TALK_MIN|provider"       /app/app/services/multi_person_pricing_policy.py /app/app/services/fusion_orchestrator.py 2>/dev/null | head -n 100 || true
  '
fi
if docker inspect df-svc-fusion-extension >/dev/null 2>&1; then
  docker exec df-svc-fusion-extension sh -lc '
    echo "[svc-fusion-extension]"
    grep -nE "^_SERVICE_ACTION|^_VARIANT_CODE|^_LEAF_SKU_CODE|^_PROVIDER"       /app/app/api/routes/v3_scene_pricing.py 2>/dev/null || true
  '
fi

echo
echo "===== 3. ACTIVE CUSTOMER VARIANTS / LEAF SKUS ====="
"${PSQL[@]}" -P pager=off -c "
select
  v.code variant_code,
  lower(v.category) category,
  v.is_active,
  vl.sku_code,
  vl.qty_mode,
  vl.qty_param,
  s.unit,
  s.provider_hint,
  s.default_unit_credits,
  s.status sku_status,
  s.metadata_json sku_metadata,
  v.metadata_json variant_metadata
from public.pricing_variants v
left join public.pricing_variant_lines vl on vl.variant_code=v.code
left join public.pricing_skus s on s.code=vl.sku_code
where v.is_active=true
  and lower(v.category) in ('face','audio','fusion','fusion_extension')
  and upper(v.code) not like '%INTERNAL%'
order by category,variant_code,sku_code;
"

echo
echo "===== 4. ACTIVE COGS COMPONENTS ====="
"${PSQL[@]}" -P pager=off -c "
select
  s.category,
  c.sku_code,
  s.provider_hint,
  s.unit,
  c.component_code,
  c.cost_model,
  c.cost_currency,
  c.variable_cost_money,
  c.fixed_monthly_cost_money,
  c.assumed_monthly_units,
  round(
    case lower(c.cost_model)
      when 'variable' then c.variable_cost_money
      when 'amortized' then case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
      when 'blended' then c.variable_cost_money + case when c.assumed_monthly_units>0 then c.fixed_monthly_cost_money/c.assumed_monthly_units else 0 end
      else 0
    end,8
  ) effective_unit_cogs,
  c.metadata_json
from public.pricing_sku_costs c
join public.pricing_skus s on s.code=c.sku_code
where c.is_active=true
  and c.effective_from<=now()
  and (c.effective_to is null or c.effective_to>now())
  and lower(s.category) in ('face','audio','fusion','fusion_extension')
order by s.category,c.sku_code,c.component_code;
"

echo
echo "===== 5. STALE / PLACEHOLDER COST MARKERS ====="
"${PSQL[@]}" -P pager=off -c "
select
  c.sku_code,
  c.component_code,
  s.provider_hint,
  c.metadata_json
from public.pricing_sku_costs c
join public.pricing_skus s on s.code=c.sku_code
where c.is_active=true
  and c.effective_from<=now()
  and (c.effective_to is null or c.effective_to>now())
  and lower(s.category) in ('face','audio','fusion','fusion_extension')
  and (
    c.component_code ~* 'fal|heygen'
    or coalesce(c.metadata_json::text,'') ~* 'fal|heygen|placeholder|update_required'
  )
order by c.sku_code,c.component_code;
"

echo
echo "===== 6. ACTIVE VARIANT RUNTIME USE — 90 DAYS ====="
"${PSQL[@]}" -P pager=off -c "
with active as (
  select code,lower(category) category
  from public.pricing_variants
  where is_active=true
    and lower(category) in ('face','audio','fusion','fusion_extension')
    and upper(code) not like '%INTERNAL%'
)
select
  a.category,
  a.code variant_code,
  count(r.*) filter (where r.created_at>=now()-interval '90 days') reservations_90d,
  count(r.*) filter (where r.created_at>=now()-interval '90 days' and r.status='committed') commits_90d,
  max(r.created_at) last_seen
from active a
left join public.pricing_credit_reservations r
  on coalesce(r.quote_json->>'variant_code',r.quote_json->>'sku_code')=a.code
group by a.category,a.code
order by a.category,a.code;
"

echo
echo "===== 7. RECENT ACTUAL BILLING ROUTES ====="
"${PSQL[@]}" -P pager=off -c "
select
  quote_json->>'service_name' service_name,
  quote_json->>'service_action' service_action,
  coalesce(quote_json->>'variant_code',quote_json->>'sku_code') variant_code,
  quote_json->>'sku_code' requested_code,
  count(*) reservations,
  count(*) filter (where status='committed') commits,
  min(created_at) first_seen,
  max(created_at) last_seen
from public.pricing_credit_reservations
where created_at>=now()-interval '30 days'
  and lower(coalesce(quote_json->>'service_name','')) in ('svc-face','svc-audio','svc-fusion','svc-fusion-extension')
group by 1,2,3,4
order by 1,2,3;
"

echo
echo "===== 8. BUSINESS-POLICY CHECK ====="
"${PSQL[@]}" -P pager=off -c "
select
  code,
  category,
  default_unit_credits,
  metadata_json->>'source_sku' source_sku,
  metadata_json->>'premium_rate_multiplier' premium_rate_multiplier,
  metadata_json->>'pricing_policy' pricing_policy
from public.pricing_skus
where code in ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
order by code;
"

echo
echo "============================================================"
echo "PROVIDER_COST_BASIS_TRUTH=COMPLETE"
echo "db_mutation=NONE"
echo "generation_mutation=NONE"
echo "production=UNTOUCHED"
echo "============================================================"
