#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${ENV_FILE:-./infra/.env}"
DATASET_ID="${1:-b4b9e391-fba5-4114-ab4c-57de7ff64ab8}"
SPLIT="${2:-test}"
LIMIT="${3:-200}"
OUT="${4:-/tmp/saree_eval_${SPLIT}_urls.csv}"

docker compose --env-file "$ENV_FILE" exec -T desifaces-db bash -lc "
psql -v ON_ERROR_STOP=1 \
  -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" \
  -v dataset_id='${DATASET_ID}' -v split='${SPLIT}' -v lim='${LIMIT}' \
<<'SQL'
COPY (
  select
    id,
    split,
    (person_ref->>'url') as person_url,
    ((garment_refs->'saree')->>'url') as saree_url,
    (conditioning_refs->'composite'->>'url') as composite_url,
    (target_ref->>'url') as target_url
  from training_examples
  where dataset_id = :'dataset_id'::uuid
    and split = :'split'
  limit :lim
) TO STDOUT WITH CSV HEADER;
SQL
" > "$OUT"

echo "✅ wrote $OUT"