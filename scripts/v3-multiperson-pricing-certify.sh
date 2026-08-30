#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE="$ROOT/scripts/v3-compose.sh"
MIGRATION="$ROOT/migrations/2026_08_30_multi_person_premium_pricing.sql"
DB_CONTAINER="desifaces-v3-db"
PRICING_CONTAINER="df-v3-svc-pricing"

banner() {
  printf '\n============================================================\n %s\n============================================================\n' "$1"
}

die() {
  echo "FAIL: $*" >&2
  exit 1
}

wait_http() {
  local name="$1" url="$2"
  local i
  for i in $(seq 1 60); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      echo "PASS: $name healthy -> $url"
      return 0
    fi
    sleep 2
  done
  die "$name did not become healthy: $url"
}

[ -x "$COMPOSE" ] || die "missing executable $COMPOSE"
[ -f "$MIGRATION" ] || die "missing migration $MIGRATION"
command -v docker >/dev/null 2>&1 || die "docker is required"
command -v curl >/dev/null 2>&1 || die "curl is required"

banner "V3 MULTI-PERSON PREMIUM PRICING CERTIFICATION"
echo "repo=$ROOT"
echo "head=$(git rev-parse --short=12 HEAD)"
echo "branch=$(git branch --show-current)"

if ! git diff --quiet -- . ':!docs/v3-core/evidence/V3-MULTIPERSON-PRICING-20260830.md'; then
  die "tracked working tree has local modifications; certify a committed tree only"
fi

banner "1. VERIFY V3 RUNTIME IDENTITY"
"$COMPOSE" up -d desifaces-db desifaces-redis svc-pricing

for _ in $(seq 1 60); do
  if docker exec "$DB_CONTAINER" sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

db_identity="$(docker exec "$DB_CONTAINER" sh -lc 'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select current_database() || chr(124) || current_user"')"
[ "$db_identity" = "desifaces_v3|desifaces_v3_admin" ] || die "refusing non-V3 database identity: $db_identity"
echo "PASS: V3 database identity=$db_identity"

banner "2. APPLY IDEMPOTENT MULTI-PERSON PRICING MIGRATION"
docker exec -i "$DB_CONTAINER" sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$MIGRATION"
echo "PASS: migration applied"

banner "3. CERTIFY LIVE CATALOG"
docker exec -i "$DB_CONTAINER" sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
DO $$
DECLARE
  sku_count integer;
  variant_count integer;
  line_count integer;
  legacy_count integer;
BEGIN
  SELECT count(*) INTO sku_count
  FROM pricing_skus
  WHERE code IN ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
    AND status = 'active';

  SELECT count(*) INTO variant_count
  FROM pricing_variants
  WHERE code IN ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
    AND is_active = true;

  SELECT count(*) INTO line_count
  FROM pricing_variant_lines
  WHERE variant_code IN ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON');

  SELECT count(*) INTO legacy_count
  FROM pricing_skus
  WHERE category IN ('face','audio','fusion')
    AND code ~ '_(MP[2-9]|MP[1-9][0-9]+)$';

  IF sku_count <> 3 THEN
    RAISE EXCEPTION 'expected 3 active multi-person SKUs, got %', sku_count;
  END IF;
  IF variant_count <> 3 THEN
    RAISE EXCEPTION 'expected 3 active multi-person variants, got %', variant_count;
  END IF;
  IF line_count <> 3 THEN
    RAISE EXCEPTION 'expected 3 multi-person variant lines, got %', line_count;
  END IF;
  IF legacy_count <> 0 THEN
    RAISE EXCEPTION 'count-specific MP SKUs detected: %', legacy_count;
  END IF;
END $$;

SELECT
  v.code AS variant_code,
  l.sku_code,
  l.qty_param,
  s.unit,
  s.default_unit_credits
FROM pricing_variants v
JOIN pricing_variant_lines l ON l.variant_code = v.code
JOIN pricing_skus s ON s.code = l.sku_code
WHERE v.code IN ('FACE_MULTI_PERSON','AUDIO_MULTI_PERSON','FUSION_MULTI_PERSON')
ORDER BY v.code;
SQL

echo "PASS: catalog has exactly one participant-agnostic SKU/variant per studio"

banner "4. BUILD ONLY AFFECTED V3 APIS"
"$COMPOSE" build svc-face svc-audio svc-fusion

banner "5. RECREATE ONLY AFFECTED V3 APIS"
"$COMPOSE" up -d --no-deps --force-recreate svc-face svc-audio svc-fusion

wait_http "svc-face" "http://127.0.0.1:18003/api/health"
wait_http "svc-audio" "http://127.0.0.1:18004/api/health"
wait_http "svc-fusion" "http://127.0.0.1:18002/api/health"
wait_http "svc-pricing" "http://127.0.0.1:18009/api/health"

banner "6. VERIFY DEPLOYED POLICY CODE"
for container in df-v3-svc-face df-v3-svc-audio df-v3-svc-fusion; do
  docker exec -i "$container" python - <<'PY'
from desifaces_shared.pricing.multi_person import select_multi_person_pricing

assert select_multi_person_pricing(
    studio="face", participant_count_value=1, natural_units=2
) is None

face2 = select_multi_person_pricing(
    studio="face", participant_count_value=2, natural_units=2
)
face5 = select_multi_person_pricing(
    studio="face", participant_count_value=5, natural_units=2
)
audio2 = select_multi_person_pricing(
    studio="audio", participant_count_value=2, natural_units=3
)
audio5 = select_multi_person_pricing(
    studio="audio", participant_count_value=5, natural_units=3
)
fusion2 = select_multi_person_pricing(
    studio="fusion", participant_count_value=2, natural_units=2
)
fusion5 = select_multi_person_pricing(
    studio="fusion", participant_count_value=5, natural_units=2
)

assert face2 and face5 and face2.sku_code == face5.sku_code == "FACE_MULTI_PERSON"
assert face2.billable_units == 4 and face5.billable_units == 10
assert audio2 and audio5 and audio2.sku_code == audio5.sku_code == "AUDIO_MULTI_PERSON"
assert audio2.billable_units == audio5.billable_units == 3
assert fusion2 and fusion5 and fusion2.sku_code == fusion5.sku_code == "FUSION_MULTI_PERSON"
assert fusion2.billable_units == 4 and fusion5.billable_units == 10
print("policy-helper=PASS")
PY
  echo "PASS: policy helper present in $container"
done

banner "7. RUN LIVE V3 DATABASE QUOTE ASSERTIONS"
docker exec -i "$PRICING_CONTAINER" python - <<'PY'
import asyncio
import os
from uuid import UUID

import asyncpg
from app.services.engine.pricing_engine import quote_variant

TEST_USER = UUID("77777777-7777-4777-8777-777777777777")

async def quote(conn, variant, params):
    return await quote_variant(
        conn,
        user_id=TEST_USER,
        variant_code=variant,
        params=params,
        channel="web",
        country_code="",
        currency="USD",
        billing_mode="bill",
    )

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            """
            insert into pricing_user_entitlements(user_id, tier_code, effective_from, metadata_json)
            values($1, 'free', now(), '{}'::jsonb)
            on conflict (user_id) do update
            set tier_code=excluded.tier_code, effective_from=excluded.effective_from
            """,
            TEST_USER,
        )

        face2 = await quote(conn, "FACE_MULTI_PERSON", {"num_edits": 4, "participant_count": 2})
        face5 = await quote(conn, "FACE_MULTI_PERSON", {"num_edits": 10, "participant_count": 5})
        audio2 = await quote(conn, "AUDIO_MULTI_PERSON", {"chars_1k": 3, "participant_count": 2})
        audio5 = await quote(conn, "AUDIO_MULTI_PERSON", {"chars_1k": 3, "participant_count": 5})
        fusion2 = await quote(conn, "FUSION_MULTI_PERSON", {"minutes": 4, "participant_count": 2})
        fusion5 = await quote(conn, "FUSION_MULTI_PERSON", {"minutes": 10, "participant_count": 5})

        assert face5.total_credits > face2.total_credits > 0
        assert audio5.total_credits == audio2.total_credits > 0
        assert fusion5.total_credits > fusion2.total_credits > 0

        baseline_face = await quote(conn, "FACE_EDIT_PREMIUM_BATCH", {"num_edits": 1})
        premium_face = await quote(conn, "FACE_MULTI_PERSON", {"num_edits": 1})
        baseline_audio = await quote(conn, "AUDIO_TTS", {"chars_1k": 1})
        premium_audio = await quote(conn, "AUDIO_MULTI_PERSON", {"chars_1k": 1})
        baseline_fusion = await quote(conn, "FUSION_TALKING_VIDEO", {"minutes": 1})
        premium_fusion = await quote(conn, "FUSION_MULTI_PERSON", {"minutes": 1})

        assert premium_face.total_credits > baseline_face.total_credits > 0
        assert premium_audio.total_credits > baseline_audio.total_credits > 0
        assert premium_fusion.total_credits > baseline_fusion.total_credits > 0

        print(
            "quotes=PASS",
            {
                "face_2": face2.total_credits,
                "face_5": face5.total_credits,
                "audio_same_chars_2": audio2.total_credits,
                "audio_same_chars_5": audio5.total_credits,
                "fusion_2": fusion2.total_credits,
                "fusion_5": fusion5.total_credits,
            },
        )
    finally:
        await tx.rollback()
        await conn.close()

asyncio.run(main())
PY

echo "PASS: live quote assertions executed in rollback-only synthetic-user transaction"

banner "8. FINAL STATUS"
echo "PASS: V3 multi-person premium pricing migration + catalog + API health + deployed policy + live quote certification"
echo "HEAD=$(git rev-parse HEAD)"
echo "No provider generation was invoked by this certification script."
