from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy-v3-admin.sh"
MIGRATION = ROOT / "migrations" / "2026_08_30_v3_admin_super_admin_role.sql"
ADMIN_SOURCE = ROOT / "services" / "svc-core" / "app" / "app" / "routes" / "admin.py"


def _text(path: Path) -> str:
    assert path.is_file(), f"missing required release artifact: {path}"
    return path.read_text(encoding="utf-8")


def test_admin_deploy_bootstrap_uses_psql_variable_and_transaction_local_setting():
    script = _text(DEPLOY)
    assert '-v "bootstrap_email=$bootstrap_email"' in script
    assert "set_config('desifaces.bootstrap_email', :'bootstrap_email', true)" in script
    assert "current_setting('desifaces.bootstrap_email', true)" in script
    assert "current_setting('BOOTSTRAP_EMAIL'" not in script
    assert "-e BOOTSTRAP_EMAIL" not in script


def test_admin_deploy_bootstrap_is_serialized_and_restart_safe():
    script = _text(DEPLOY)
    assert "SELECT pg_advisory_xact_lock(86300830);" in script
    assert "ON CONFLICT(user_id, role_id) DO NOTHING" in script
    assert "active_super_admins_before" in script
    assert "active_super_admins_after" in script
    assert "V3_SUPER_ADMIN_EMAIL" in script


def test_runtime_governance_uses_same_advisory_lock_as_bootstrap():
    admin_source = _text(ADMIN_SOURCE)
    deploy = _text(DEPLOY)
    assert "ADMIN_GOVERNANCE_LOCK_KEY = 86300830" in admin_source
    assert "pg_advisory_xact_lock($1)" in admin_source
    assert "await _lock_admin_governance(conn)" in admin_source
    assert "pg_advisory_xact_lock(86300830)" in deploy
    assert "and bool(target[\"is_active\"])" in admin_source
    assert "and bool(before_row[\"is_active\"])" in admin_source


def test_admin_deploy_only_recreates_core_and_never_tears_down_v3():
    script = _text(DEPLOY)
    assert "./scripts/v3-compose.sh build svc-core" in script
    assert "./scripts/v3-compose.sh up -d --no-deps svc-core" in script
    assert "v3-compose.sh down" not in script
    assert "docker compose down" not in script


def test_admin_deploy_has_fail_closed_and_contract_smoke_gates():
    script = _text(DEPLOY)
    assert '"$CORE_URL/api/admin/context"' in script
    assert 'if [[ "$status" != "401" ]]' in script
    for route in (
        "/api/admin/context",
        "/api/admin/users",
        "/api/admin/access/administrators",
        "/api/admin/support/requests",
        "/api/admin/audit",
    ):
        assert route in script


def test_super_admin_role_migration_is_idempotent_and_uses_existing_rbac():
    migration = _text(MIGRATION)
    assert "INSERT INTO core.roles" in migration
    assert "'super_admin'" in migration
    assert "ON CONFLICT (role_key) DO UPDATE" in migration
    assert "CREATE TABLE" not in migration.upper()
