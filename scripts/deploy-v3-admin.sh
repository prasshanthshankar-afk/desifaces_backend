#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_CONTAINER="${DB_CONTAINER:-desifaces-v3-db}"
CORE_CONTAINER="${CORE_CONTAINER:-df-v3-svc-core}"
CORE_URL="${CORE_URL:-http://127.0.0.1:18000}"
DB_USER="${DB_USER:-desifaces_v3_admin}"
DB_NAME="${DB_NAME:-desifaces_v3}"
MIGRATION="migrations/2026_08_30_v3_admin_super_admin_role.sql"
CERTIFIED_ADMIN_COMMIT="${CERTIFIED_ADMIN_COMMIT:-fc960a38109d2180f131c0394a2101dc41b82459}"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
need git
need docker
need curl

printf '\n============================================================\n'
printf ' desifaces V3 ADMIN — CORE DEPLOY + CERTIFY\n'
printf '============================================================\n'

printf '\n===== 1. VERIFY SOURCE =====\n'
HEAD="$(git rev-parse HEAD)"
BRANCH="$(git branch --show-current || true)"
printf 'branch=%s\nhead=%s\n' "${BRANCH:-DETACHED}" "$HEAD"
if ! git cat-file -e "${CERTIFIED_ADMIN_COMMIT}^{commit}" 2>/dev/null; then
  echo "FAIL: Admin certification commit not available locally: $CERTIFIED_ADMIN_COMMIT" >&2
  exit 3
fi
if ! git merge-base --is-ancestor "$CERTIFIED_ADMIN_COMMIT" "$HEAD"; then
  echo "FAIL: current backend source does not contain Admin certification commit $CERTIFIED_ADMIN_COMMIT" >&2
  exit 4
fi
if ! git diff --quiet; then
  echo 'FAIL: tracked backend source has local modifications. Refusing ambiguous deployment.' >&2
  git status --short
  exit 5
fi
[[ -f "$MIGRATION" ]] || { echo "FAIL: missing $MIGRATION" >&2; exit 6; }
[[ -x scripts/v3-compose.sh ]] || { echo 'FAIL: scripts/v3-compose.sh is not executable' >&2; exit 7; }

docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: V3 DB container not found: $DB_CONTAINER" >&2; exit 8; }

psql_v3(){ docker exec -i "$DB_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" "$@"; }
psql_scalar(){ docker exec -i "$DB_CONTAINER" psql -Atq -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -c "$1"; }

printf '\n===== 2. APPLY IDEMPOTENT ADMIN ROLE MIGRATION =====\n'
psql_v3 < "$MIGRATION"
role_count="$(psql_scalar "SELECT count(*) FROM core.roles WHERE role_key='super_admin';")"
printf 'super_admin_role_rows=%s\n' "$role_count"
[[ "$role_count" == "1" ]] || { echo 'FAIL: super_admin role migration did not converge to one role row.' >&2; exit 9; }

printf '\n===== 3. ENSURE GOVERNANCE BOOTSTRAP =====\n'
super_count="$(psql_scalar "SELECT count(DISTINCT u.id) FROM core.users u JOIN core.user_roles ur ON ur.user_id=u.id JOIN core.roles r ON r.id=ur.role_id WHERE u.is_active=true AND r.role_key='super_admin';")"
printf 'active_super_admins_before=%s\n' "$super_count"

if [[ "$super_count" == "0" ]]; then
  bootstrap_email="${V3_SUPER_ADMIN_EMAIL:-}"
  if [[ -z "$bootstrap_email" ]]; then
    admin_count="$(psql_scalar "SELECT count(DISTINCT u.id) FROM core.users u JOIN core.user_roles ur ON ur.user_id=u.id JOIN core.roles r ON r.id=ur.role_id WHERE u.is_active=true AND r.role_key='admin';")"
    if [[ "$admin_count" == "1" ]]; then
      bootstrap_email="$(psql_scalar "SELECT u.email FROM core.users u JOIN core.user_roles ur ON ur.user_id=u.id JOIN core.roles r ON r.id=ur.role_id WHERE u.is_active=true AND r.role_key='admin' LIMIT 1;")"
      printf 'bootstrap_source=sole_active_admin\n'
    else
      echo "FAIL: no active super_admin exists and active_admin_count=$admin_count." >&2
      echo 'Set V3_SUPER_ADMIN_EMAIL to one existing active Admin user and rerun this same script.' >&2
      exit 10
    fi
  else
    printf 'bootstrap_source=V3_SUPER_ADMIN_EMAIL\n'
  fi

  if [[ -z "${bootstrap_email//[[:space:]]/}" ]]; then
    echo 'FAIL: resolved Super Admin bootstrap email is empty.' >&2
    exit 10
  fi

  # psql does not interpolate variables inside dollar-quoted PL/pgSQL bodies.
  # Copy the selected email into a transaction-local custom setting first, then
  # read that setting from the DO block. No container environment transport is used.
  docker exec -i \
    "$DB_CONTAINER" \
    psql -v ON_ERROR_STOP=1 -v "bootstrap_email=$bootstrap_email" -U "$DB_USER" -d "$DB_NAME" <<'SQL'
BEGIN;
SELECT pg_advisory_xact_lock(86300830);
SELECT set_config('desifaces.bootstrap_email', :'bootstrap_email', true);
DO $$
DECLARE
  target_id uuid;
  target_email text;
  super_admin_role_id bigint;
  existing_count integer;
  before_roles jsonb;
  after_roles jsonb;
BEGIN
  SELECT count(DISTINCT u.id)
    INTO existing_count
  FROM core.users u
  JOIN core.user_roles ur ON ur.user_id=u.id
  JOIN core.roles r ON r.id=ur.role_id
  WHERE u.is_active=true AND r.role_key='super_admin';
  IF existing_count > 0 THEN
    RETURN;
  END IF;

  SELECT id, email INTO target_id, target_email
  FROM core.users
  WHERE lower(email)=lower(current_setting('desifaces.bootstrap_email', true))
    AND is_active=true
  FOR UPDATE;
  IF target_id IS NULL THEN
    RAISE EXCEPTION 'bootstrap target is not an active Core user';
  END IF;

  SELECT id INTO super_admin_role_id FROM core.roles WHERE role_key='super_admin';
  IF super_admin_role_id IS NULL THEN
    RAISE EXCEPTION 'super_admin role missing';
  END IF;

  SELECT coalesce(jsonb_agg(r.role_key ORDER BY r.role_key), '[]'::jsonb)
    INTO before_roles
  FROM core.user_roles ur JOIN core.roles r ON r.id=ur.role_id
  WHERE ur.user_id=target_id;

  INSERT INTO core.user_roles(user_id, role_id)
  VALUES(target_id, super_admin_role_id)
  ON CONFLICT(user_id, role_id) DO NOTHING;

  SELECT coalesce(jsonb_agg(r.role_key ORDER BY r.role_key), '[]'::jsonb)
    INTO after_roles
  FROM core.user_roles ur JOIN core.roles r ON r.id=ur.role_id
  WHERE ur.user_id=target_id;

  INSERT INTO core.audit_log(actor_user_id,action,entity_type,entity_id,request_id,before_json,after_json,user_agent)
  VALUES(NULL,'admin.super_admin.bootstrap','user_role',target_id::text,'deploy-v3-admin',jsonb_build_object('roles',before_roles),jsonb_build_object('roles',after_roles,'email',target_email,'method','controlled_deploy_bootstrap'),'deploy-v3-admin.sh');
END $$;
COMMIT;
SQL
fi

super_count="$(psql_scalar "SELECT count(DISTINCT u.id) FROM core.users u JOIN core.user_roles ur ON ur.user_id=u.id JOIN core.roles r ON r.id=ur.role_id WHERE u.is_active=true AND r.role_key='super_admin';")"
printf 'active_super_admins_after=%s\n' "$super_count"
if [[ "$super_count" -lt 1 ]]; then
  echo 'FAIL: no active super_admin exists after bootstrap gate.' >&2
  exit 11
fi

printf '\n===== 4. BUILD + RECREATE ONLY SVC-CORE =====\n'
./scripts/v3-compose.sh build svc-core
./scripts/v3-compose.sh up -d --no-deps svc-core

printf '\n===== 5. WAIT FOR CORE =====\n'
status="000"
for _ in $(seq 1 30); do
  status="$(curl -sS -o /tmp/v3-admin-core-health.json -w '%{http_code}' "$CORE_URL/api/health" || true)"
  [[ "$status" == "200" ]] && break
  sleep 2
done
printf 'core_health_http=%s\n' "${status:-000}"
if [[ "${status:-000}" != "200" ]]; then
  docker ps -a --filter "name=^/${CORE_CONTAINER}$"
  docker logs --tail 160 "$CORE_CONTAINER" || true
  echo 'FAIL: svc-core did not become healthy.' >&2
  exit 12
fi

printf '\n===== 6. FAIL-CLOSED ADMIN API SMOKE =====\n'
status="$(curl -sS -o /tmp/v3-admin-core-unauth.json -w '%{http_code}' "$CORE_URL/api/admin/context")"
printf 'GET /api/admin/context without auth -> HTTP %s\n' "$status"
if [[ "$status" != "401" ]]; then
  cat /tmp/v3-admin-core-unauth.json || true
  echo 'FAIL: unauthenticated Core Admin context did not return 401.' >&2
  exit 13
fi

printf '\n===== 7. CONTRACT PRESENCE =====\n'
openapi="$(curl -fsS "$CORE_URL/openapi.json")"
for path in \
  '/api/admin/context' \
  '/api/admin/users' \
  '/api/admin/access/administrators' \
  '/api/admin/support/requests' \
  '/api/admin/audit'; do
  if ! grep -Fq "\"$path\"" <<<"$openapi"; then
    echo "FAIL: deployed Core OpenAPI missing $path" >&2
    exit 14
  fi
  printf 'route_present=%s\n' "$path"
done

printf '\n===== 8. DEPLOYMENT EVIDENCE =====\n'
docker ps --filter "name=^/${CORE_CONTAINER}$" --format 'container={{.Names}} status={{.Status}} image={{.Image}}'
printf 'source_head=%s\n' "$HEAD"
printf 'certified_admin_commit=%s\n' "$CERTIFIED_ADMIN_COMMIT"
printf 'active_super_admins=%s\n' "$super_count"
printf 'core_unauth_admin_context=401\n'
printf '\nPASS: V3 Core Admin role migration, governance bootstrap, svc-core deployment and fail-closed smoke checks passed.\n'
