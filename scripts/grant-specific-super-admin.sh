#!/usr/bin/env bash
set -euo pipefail

TARGET_EMAIL="${TARGET_EMAIL:-df-sadmin@desifaces.ai}"
DB_CONTAINER="${DB_CONTAINER:-desifaces-v3-db}"
DB_USER="${DB_USER:-desifaces_v3_admin}"
DB_NAME="${DB_NAME:-desifaces_v3}"
LOCK_ID="86300830"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "FAIL: missing required command: $1" >&2; exit 2; }; }
need docker

printf '\n============================================================\n'
printf ' desifaces V3 — AUDITED SUPER ADMIN GRANT\n'
printf '============================================================\n'
printf 'target=%s\n' "$TARGET_EMAIL"

docker inspect "$DB_CONTAINER" >/dev/null 2>&1 || { echo "FAIL: V3 DB container not found: $DB_CONTAINER" >&2; exit 3; }

# The operation is deliberately narrow and idempotent. It never changes account
# activation, never removes another administrator, and never guesses a target.
docker exec -i "$DB_CONTAINER" \
  psql -v ON_ERROR_STOP=1 \
       -v "target_email=$TARGET_EMAIL" \
       -v "lock_id=$LOCK_ID" \
       -U "$DB_USER" -d "$DB_NAME" <<'SQL'
BEGIN;
SELECT pg_advisory_xact_lock(:'lock_id'::bigint);
SELECT set_config('desifaces.target_super_admin_email', :'target_email', true);

DO $$
DECLARE
  target_id uuid;
  canonical_email text;
  super_role_id bigint;
  before_roles jsonb;
  after_roles jsonb;
BEGIN
  SELECT id, email
    INTO target_id, canonical_email
  FROM core.users
  WHERE lower(email)=lower(current_setting('desifaces.target_super_admin_email', true))
    AND is_active=true
  FOR UPDATE;

  IF target_id IS NULL THEN
    RAISE EXCEPTION 'target account is missing or inactive: %', current_setting('desifaces.target_super_admin_email', true);
  END IF;

  SELECT id INTO super_role_id
  FROM core.roles
  WHERE role_key='super_admin';

  IF super_role_id IS NULL THEN
    RAISE EXCEPTION 'super_admin role is missing';
  END IF;

  SELECT coalesce(jsonb_agg(r.role_key ORDER BY r.role_key), '[]'::jsonb)
    INTO before_roles
  FROM core.user_roles ur
  JOIN core.roles r ON r.id=ur.role_id
  WHERE ur.user_id=target_id;

  INSERT INTO core.user_roles(user_id, role_id)
  VALUES(target_id, super_role_id)
  ON CONFLICT(user_id, role_id) DO NOTHING;

  SELECT coalesce(jsonb_agg(r.role_key ORDER BY r.role_key), '[]'::jsonb)
    INTO after_roles
  FROM core.user_roles ur
  JOIN core.roles r ON r.id=ur.role_id
  WHERE ur.user_id=target_id;

  INSERT INTO core.audit_log(
    actor_user_id, action, entity_type, entity_id, request_id,
    before_json, after_json, user_agent
  ) VALUES (
    NULL,
    'admin.super_admin.controlled_bootstrap',
    'user_role',
    target_id::text,
    'grant-specific-super-admin',
    jsonb_build_object('roles', before_roles),
    jsonb_build_object('roles', after_roles, 'email', canonical_email, 'method', 'controlled_ops_bootstrap'),
    'grant-specific-super-admin.sh'
  );
END $$;
COMMIT;

SELECT u.email, string_agg(r.role_key, ',' ORDER BY r.role_key) AS roles
FROM core.users u
JOIN core.user_roles ur ON ur.user_id=u.id
JOIN core.roles r ON r.id=ur.role_id
WHERE lower(u.email)=lower(:'target_email')
GROUP BY u.email;
SQL

printf '\nPASS: %s now has super_admin through an audited, idempotent bootstrap.\n' "$TARGET_EMAIL"
