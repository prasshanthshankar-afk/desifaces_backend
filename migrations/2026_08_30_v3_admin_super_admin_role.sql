-- V3 Admin governance: introduce super_admin as an extension of existing Core RBAC.
-- No parallel entitlement/admin table is created.

BEGIN;

INSERT INTO core.roles(role_key, description)
VALUES ('super_admin', 'Administrator access governance and operational administration')
ON CONFLICT (role_key) DO UPDATE
SET description = EXCLUDED.description;

COMMIT;
