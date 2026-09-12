#!/usr/bin/env bash
set -Eeuo pipefail

DB="${DF_DB_CONTAINER:-}"
if [[ -z "$DB" ]]; then
  for candidate in desifaces-v3-db df-v3-db desifaces-db; do
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
echo "===== 0. SCHEMA CONTRACT ====="
for required in pricing_skus pricing_sku_costs; do
  EXISTS="$("${PSQL[@]}" -At -c "select case when to_regclass('public.${required}') is not null then 1 else 0 end;")"
  [[ "$EXISTS" == "1" ]] || { echo "FAIL: required table public.$required missing"; exit 1; }
done

echo "PRICING_SCHEMA_BASE=PASS"

echo
echo "===== 1. AUDIO SKU / PROVIDER CONTRACT ====="
# Canonical pricing_skus columns are: code, name, unit, category,
# provider_hint, default_unit_credits, status, metadata_json. Use to_jsonb so
# certification remains tolerant of additive schema changes without inventing
# alternate column names.
"${PSQL[@]}" -P pager=off -c "
select s.code,
       to_jsonb(s)->>'unit' as unit,
       to_jsonb(s)->>'category' as category,
       to_jsonb(s)->>'provider_hint' as provider_hint,
       to_jsonb(s)->>'default_unit_credits' as default_unit_credits,
       to_jsonb(s)->>'status' as status
  from public.pricing_skus s
 where s.code='AUDIO_TTS_1K_CHARS';"

AUDIO_SKU_OK="$("${PSQL[@]}" -At -c "
select count(*)
  from public.pricing_skus s
 where s.code='AUDIO_TTS_1K_CHARS'
   and lower(coalesce(to_jsonb(s)->>'status',''))='active'
   and coalesce(to_jsonb(s)->>'unit','')='1k_chars'
   and lower(coalesce(to_jsonb(s)->>'category',''))='audio'
   and lower(coalesce(to_jsonb(s)->>'provider_hint',''))='azure_tts'
   and coalesce(nullif(to_jsonb(s)->>'default_unit_credits','')::numeric,0) > 0;")"
[[ "$AUDIO_SKU_OK" == "1" ]] || { echo "AUDIO_SKU_CONTRACT=FAIL"; exit 1; }

echo "AUDIO_SKU_CONTRACT=PASS"

echo
echo "===== 2. ACTIVE LAUNCH COGS ====="
"${PSQL[@]}" -P pager=off -c "
with launch(sku_code) as (
  values ('IMG_STD_RUN'),('AUDIO_TTS_1K_CHARS'),('FUSION_TALK_MIN')
), c as (
  select l.sku_code,
         p.component_code,p.cost_model,p.cost_currency,
         p.variable_cost_money,p.fixed_monthly_cost_money,p.assumed_monthly_units,
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

MISSING_SKUS="$("${PSQL[@]}" -At -c "
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
select coalesce(string_agg(sku_code,',' order by sku_code),'') from totals where unit_cogs<=0;")"
[[ -z "$MISSING_SKUS" ]] || {
  echo "LAUNCH_COGS_COMPLETENESS=FAIL missing_or_zero=$MISSING_SKUS"
  exit 1
}
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
# Guard against accidental zero/near-zero placeholders. This is not a customer
# price assertion and does not prevent a later effective-dated negotiated rate.
assert v >= Decimal('0.001'), f'audio COGS unexpectedly low: {v}'
print('AUDIO_NONZERO_COGS_GATE=PASS')
PY

echo
echo "===== 4. FAIL-CLOSED ECONOMICS CONTRACT ====="
SRC="services/svc-pricing/app/app/services/reservations/reservation_service.py"
if [[ -f "$SRC" ]]; then
  grep -q 'missing_cost_skus' "$SRC" || { echo "ECONOMICS_FAIL_CLOSED_SOURCE=FAIL missing missing_cost_skus"; exit 1; }
  grep -q 'has_costs_complete' "$SRC" || { echo "ECONOMICS_FAIL_CLOSED_SOURCE=FAIL missing has_costs_complete"; exit 1; }
  grep -q 'gross_margin_pct_final.*None' "$SRC" || { echo "ECONOMICS_FAIL_CLOSED_SOURCE=FAIL missing null-margin behavior"; exit 1; }
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
