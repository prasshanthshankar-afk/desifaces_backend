#!/usr/bin/env bash
set -euo pipefail

# V3-C3 authenticated, read-only cross-capability adapter certification.
#
# The certification deliberately performs no generation, provider execution,
# pricing operation, reservation, ledger mutation, media write, bootstrap, or
# worker/reconciler activation. Tokens and secrets are never printed.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

for cmd in docker curl jq; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "C3_CERT_FAIL=missing_command:$cmd" >&2
    exit 1
  }
done

for container in desifaces-v3-db df-v3-svc-face df-v3-svc-audio df-v3-svc-fusion df-v3-svc-pricing; do
  docker inspect "$container" >/dev/null 2>&1 || {
    echo "C3_CERT_FAIL=missing_container:$container" >&2
    exit 1
  }
done

# Mirror desifaces_shared.identity.resolve_account_context exactly enough to
# choose a certifiable user/account pair:
#   1. active membership
#   2. pricing_credit_accounts.billing_account_id
#   3. active individual account_code=user:<uuid>
IDENTITY_ROW="$(
  docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL' | tail -n 1
WITH candidates AS (
    SELECT
        bam.user_id,
        bam.billing_account_id,
        1 AS priority,
        CASE WHEN bam.is_default THEN 0 ELSE 1 END AS default_rank,
        CASE bam.role
          WHEN 'owner' THEN 0
          WHEN 'finance_admin' THEN 1
          WHEN 'member' THEN 2
          WHEN 'viewer' THEN 3
          ELSE 4
        END AS role_rank,
        bam.created_at AS created_rank
    FROM public.pricing_billing_account_members bam
    JOIN public.pricing_billing_accounts ba
      ON ba.id = bam.billing_account_id
    JOIN core.users u
      ON u.id = bam.user_id
    WHERE bam.status = 'active'
      AND ba.status = 'active'

    UNION ALL

    SELECT
        pca.user_id,
        pca.billing_account_id,
        2 AS priority,
        0 AS default_rank,
        0 AS role_rank,
        NULL::timestamptz AS created_rank
    FROM public.pricing_credit_accounts pca
    JOIN public.pricing_billing_accounts ba
      ON ba.id = pca.billing_account_id
    JOIN core.users u
      ON u.id = pca.user_id
    WHERE pca.billing_account_id IS NOT NULL
      AND ba.status = 'active'

    UNION ALL

    SELECT
        u.id AS user_id,
        ba.id AS billing_account_id,
        3 AS priority,
        0 AS default_rank,
        0 AS role_rank,
        NULL::timestamptz AS created_rank
    FROM core.users u
    JOIN public.pricing_billing_accounts ba
      ON ba.account_code = 'user:' || u.id::text
    WHERE ba.status = 'active'
)
SELECT user_id::text || '|' || billing_account_id::text
FROM candidates
ORDER BY priority, default_rank, role_rank, created_rank NULLS LAST, user_id
LIMIT 1;
SQL
)"

if [[ -z "$IDENTITY_ROW" || "$IDENTITY_ROW" != *"|"* ]]; then
  echo "C3_CERT_FAIL=no_active_v3_user_account_context" >&2
  docker exec -i desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL' >&2 || true
SELECT
  'users=' || (SELECT count(*) FROM core.users)::text ||
  ' billing_accounts=' || (SELECT count(*) FROM public.pricing_billing_accounts)::text ||
  ' active_billing_accounts=' || (SELECT count(*) FROM public.pricing_billing_accounts WHERE status='active')::text ||
  ' active_memberships=' || (SELECT count(*) FROM public.pricing_billing_account_members WHERE status='active')::text ||
  ' credit_account_links=' || (SELECT count(*) FROM public.pricing_credit_accounts WHERE billing_account_id IS NOT NULL)::text ||
  ' user_code_accounts=' || (
      SELECT count(*)
      FROM core.users u
      JOIN public.pricing_billing_accounts ba
        ON ba.account_code = 'user:' || u.id::text
      WHERE ba.status='active'
  )::text;
SQL
  exit 1
fi

IFS='|' read -r USER_ID ACCOUNT_ID <<<"$IDENTITY_ROW"

mint_user_token() {
  local container="$1"
  docker exec -e C3_USER_ID="$USER_ID" "$container" python -c '
import os, time
from jose import jwt
uid = os.environ["C3_USER_ID"]
secret = os.getenv("JWT_SECRET") or os.getenv("JWT_HMAC_SECRET") or ""
if not secret:
    raise SystemExit("JWT secret unavailable")
alg = os.getenv("JWT_ALG", "HS256")
now = int(time.time())
claims = {"sub": uid, "iat": now, "exp": now + 300}
issuer = os.getenv("JWT_ISSUER")
audience = os.getenv("JWT_AUDIENCE")
if issuer:
    claims["iss"] = issuer
if audience:
    claims["aud"] = audience
print(jwt.encode(claims, secret, algorithm=alg))
'
}

FACE_TOKEN="$(mint_user_token df-v3-svc-face)"
AUDIO_TOKEN="$(mint_user_token df-v3-svc-audio)"
FUSION_TOKEN="$(mint_user_token df-v3-svc-fusion)"

snapshot_user_state() {
  docker exec -i -e C3_USER_ID="$USER_ID" desifaces-v3-db sh -lc 'psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v uid="$C3_USER_ID"' <<'SQL' | tail -n 1
SELECT
  (SELECT count(*) FROM public.studio_jobs WHERE user_id = :'uid'::uuid)::text || '|' ||
  (SELECT count(*) FROM public.pricing_credit_reservations WHERE user_id = :'uid'::uuid)::text || '|' ||
  (SELECT count(*) FROM public.pricing_credit_ledger_events WHERE user_id = :'uid'::uuid)::text || '|' ||
  (SELECT count(*) FROM public.media_assets WHERE user_id = :'uid'::uuid)::text || '|' ||
  COALESCE((
    SELECT balance_credits::text || ':' || reserved_credits::text
    FROM public.pricing_credit_accounts
    WHERE user_id = :'uid'::uuid
    LIMIT 1
  ), 'none');
SQL
}

BEFORE_STATE="$(snapshot_user_state)"

FACE_JSON="$(curl -fsS -X POST \
  'http://127.0.0.1:18003/internal/v3/face-adapter/map' \
  -H "Authorization: Bearer $FACE_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"mode":"text-to-image","user_prompt":"v3-c3-read-only-certification","num_variants":1}')"

AUDIO_JSON="$(curl -fsS -X POST \
  'http://127.0.0.1:18004/internal/v3/audio-adapter/map' \
  -H "Authorization: Bearer $AUDIO_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"text":"V3 C3 read only certification","target_locale":"en-US","source_language":"en","translate":false,"voice_id":"v3-c3-cert-placeholder","output_format":"mp3"}')"

FUSION_JSON="$(curl -fsS -X POST \
  'http://127.0.0.1:18002/internal/v3/fusion-adapter/map' \
  -H "Authorization: Bearer $FUSION_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"voice_mode":"audio","video":{"aspect_ratio":"9:16","duration_sec":10},"consent":{"external_provider_ok":false}}')"

PRICING_FINGERPRINT="cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
PRICING_JSON="$(curl -fsS -X POST \
  'http://127.0.0.1:18009/internal/v3/pricing-adapter/map-preview' \
  -H "Authorization: Bearer $FACE_TOKEN" \
  -H "X-User-Id: $USER_ID" \
  -H 'Content-Type: application/json' \
  --data "{\"status\":\"quoted\",\"quote_id\":\"qt_v3c3_readonly\",\"preview_fingerprint\":\"$PRICING_FINGERPRINT\",\"service_name\":\"svc-face\",\"service_action\":\"face.creator.generate\",\"sku_code\":\"FACE_CREATOR\",\"currency\":\"USD\",\"estimated_amount\":\"0.00\",\"quote_breakdown\":{\"total_credits\":\"1\",\"total_money\":\"0.00\",\"currency\":\"USD\",\"pricebook_revision\":\"v3-c3-readonly\"}}")"

jq -e --arg uid "$USER_ID" --arg aid "$ACCOUNT_ID" '
  .generation_request.requested_by_user_id == $uid and
  .generation_request.account_id == $aid and
  .generation_request.kind == "face" and
  .request_context.actor.actor_id == $uid
' <<<"$FACE_JSON" >/dev/null
echo "FACE_AUTHENTICATED_MAPPING=PASS"

jq -e --arg uid "$USER_ID" --arg aid "$ACCOUNT_ID" '
  .generation_request.requested_by_user_id == $uid and
  .generation_request.account_id == $aid and
  .generation_request.kind == "audio" and
  .request_context.actor.actor_id == $uid
' <<<"$AUDIO_JSON" >/dev/null
echo "AUDIO_AUTHENTICATED_MAPPING=PASS"

jq -e --arg uid "$USER_ID" --arg aid "$ACCOUNT_ID" '
  .generation_request.requested_by_user_id == $uid and
  .generation_request.account_id == $aid and
  .generation_request.kind == "fusion" and
  .request_context.actor.actor_id == $uid
' <<<"$FUSION_JSON" >/dev/null
echo "FUSION_AUTHENTICATED_MAPPING=PASS"

jq -e --arg uid "$USER_ID" --arg aid "$ACCOUNT_ID" --arg fp "$PRICING_FINGERPRINT" '
  .quote.user_id == $uid and
  .quote.account_id == $aid and
  .quote.fingerprint == $fp and
  .legacy_quote_id == "qt_v3c3_readonly"
' <<<"$PRICING_JSON" >/dev/null
echo "PRICING_AUTHENTICATED_MAPPING=PASS"

AFTER_STATE="$(snapshot_user_state)"

if [[ "$BEFORE_STATE" != "$AFTER_STATE" ]]; then
  echo "C3_CERT_FAIL=selected_user_state_changed" >&2
  echo "C3_PERSISTENCE_INVARIANTS=FAIL" >&2
  exit 1
fi

echo "C3_SHARED_USER_ACCOUNT_CONTEXT=PASS"
echo "C3_PERSISTENCE_INVARIANTS=PASS"

if docker ps --format '{{.Names}}' | grep -Eq '^df-v3-.*(worker|scheduler)'; then
  echo "C3_CERT_FAIL=v3_execution_worker_or_scheduler_active" >&2
  exit 1
fi

echo "C3_EXECUTION_GUARD=PASS"
echo "V3_C3_AUTHENTICATED_CRITICAL_PATH_CERTIFICATION=PASS"
