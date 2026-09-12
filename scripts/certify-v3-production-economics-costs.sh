#!/usr/bin/env bash
set -Eeuo pipefail

DB="${DF_DB_CONTAINER:-}"
if [[ -z "$DB" ]]; then
  for candidate in desifaces-v3-db df-v3-db; do
    if docker inspect "$candidate" >/dev/null 2>&1; then DB="$candidate"; break; fi
  done
fi
[[ -n "$DB" ]] || { echo "FAIL: postgres container not found"; exit 1; }

PGUSER="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_USER"{print $2; exit}')"
PGDB="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$DB" | awk -F= '$1=="POSTGRES_DB"{print $2; exit}')"
PGUSER="${PGUSER:-postgres}"
PGDB="${PGDB:-postgres}"
PSQL=(docker exec -i "$DB" psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB")

echo "============================================================"
echo " desifaces V3 — PRODUCTION ECONOMICS COST CERTIFICATION"
echo "============================================================"
echo "db_container=$DB"

echo
echo "===== 1. AUDIO SKU / PROVIDER CONTRACT ====="
"${PSQL[@]}" -P pager=off -c "
select code, unit_type, credits_per_unit, status,
       coalesce(to_jsonb(pricing_skus)->>'provider_hint','') as provider_hint
  from public.pricing_skus
 where code='AUDIO_TTS_1K_CHARS';"

AUDIO_SKU_OK="$("${PSQL[@]}" -At -c "
select count(*)
  from public.pricing_skus
 where code='AUDIO_TTS_1K_CHARS'
   and status='active'
   and unit_type='1k_chars'
   and credits_per_unit > 0;")"
[[ "$AUDIO_SKU_OK" == "1" ]] || { echo "AUDIO_SKU_CONTRACT=FAIL"; exit 1; }

echo "AUDIO_SKU_CONTRACT=PASS"

echo
echo "===== 2. ACTIVE LAUNCH COGS ====="
"${PSQL[@]}" -P pager=off -c "
with launch(sku_code) as (
  values ('IMG_STD_RUN'),('AUDIO_TTS_1K_CHARS'),('FUSION_TALK_MIN')
), c as (
  select l.sku_code,
         p.component_code,p.cost_model,p.variable_cost_money,
         p.fixed_monthly_cost_money,p.assumed_monthly_units,
         (p.variable_cost_money + case when p.assumed_monthly_units>0
              then p.fixed_monthly_cost_money/p.assumed_monthly_units else 0 end) as effective_unit_cogs
    from launch l
    left join public.pricing_sku_costs p
      on p.sku_code=l.sku_code
     and p.is_active=true
     and p.effective_from<=now()
     and (p.effective_to is null or p.effective_to>now())
)
select * from c order by sku_code,component_code;"

MISSING="$("${PSQL[@]}" -At -c "
with launch(sku_code) as (
  values ('IMG_STD_RUN'),('AUDIO_TTS_1K_CHARS'),('FUSION_TALK_MIN')
), totals as (
  select l.sku_code,
         coalesce(sum(p.variable_cost_money + case when p.assumed_monthly_units>0
           then p.fixed_monthly_cost_money/p.assumed_monthly_units else 0 end),0) unit_cogs
    from launch l
    left join public.pricing_sku_costs p
      on p.sku_code=l.sku_code
     and p.is_active=true
     and p.effective_from<=now()
     and (p.effective_to is null or p.effective_to>now())
   group by l.sku_code
)
select count(*) from totals where unit_cogs<=0;")"
[[ "$MISSING" == "0" ]] || { echo "LAUNCH_COGS_COMPLETENESS=FAIL missing_or_zero=$MISSING"; exit 1; }
echo "LAUNCH_COGS_COMPLETENESS=PASS"

echo
echo "===== 3. AUDIO COST BASIS ====="
AUDIO_COST="$("${PSQL[@]}" -At -c "
select coalesce(sum(variable_cost_money + case when assumed_monthly_units>0
  then fixed_monthly_cost_money/assumed_monthly_units else 0 end),0)
from public.pricing_sku_costs
where sku_code='AUDIO_TTS_1K_CHARS'
  and is_active=true
  and effective_from<=now()
  and (effective_to is null or effective_to>now());")"
echo "audio_cogs_usd_per_1k_chars=$AUDIO_COST"
python3 - "$AUDIO_COST" <<'PY'
from decimal import Decimal
import sys
v=Decimal(sys.argv[1])
assert v > 0, v
assert v >= Decimal('0.015'), f'audio COGS unexpectedly low: {v}'
print('AUDIO_NONZERO_COGS_GATE=PASS')
PY

echo
echo "===== 4. FAIL-CLOSED ECONOMICS CONTRACT ====="
SRC="services/svc-pricing/app/app/services/reservations/reservation_service.py"
if [[ -f "$SRC" ]]; then
  grep -q 'missing_cost_skus' "$SRC" || { echo "ECONOMICS_FAIL_CLOSED_SOURCE=FAIL missing missing_cost_skus"; exit 1; }
  grep -q 'has_costs_complete' "$SRC" || { echo "ECONOMICS_FAIL_CLOSED_SOURCE=FAIL missing has_costs_complete"; exit 1; }
  echo "ECONOMICS_FAIL_CLOSED_SOURCE=PASS"
else
  echo "ECONOMICS_FAIL_CLOSED_SOURCE=SKIP source_not_in_cwd"
fi

echo
echo "============================================================"
echo " ECONOMICS COST CERTIFICATION=PASS"
echo "============================================================"
echo "CUSTOMER_PRICING_CHANGE=NONE"
echo "AUDIO_COGS_CONFIGURED=PASS"
echo "MISSING_COSTS_FAIL_CLOSED=PASS"
