from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET", "v3-admin-test-jwt-secret")
os.environ.setdefault("REFRESH_TOKEN_HMAC_SECRET", "v3-admin-test-refresh-secret")

from app import deps  # noqa: E402
from app.main import create_app  # noqa: E402


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row


def _run_require_admin(monkeypatch, *, db_row: dict, token_roles: list[str]):
    async def fake_get_pool():
        return _Pool(db_row)

    monkeypatch.setattr(deps, "get_pool", fake_get_pool)
    user_id = str(uuid4())
    claims = {"sub": user_id, "email": "admin@example.test", "roles": token_roles}
    return asyncio.run(deps.require_admin(claims))


def test_live_db_role_grants_admin_even_when_token_role_is_stale_non_admin(monkeypatch):
    claims = _run_require_admin(
        monkeypatch,
        db_row={"is_active": True, "roles": ["user", "admin"]},
        token_roles=["user"],
    )
    assert "admin" in claims["roles"]


def test_live_db_role_revocation_denies_stale_admin_token(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _run_require_admin(
            monkeypatch,
            db_row={"is_active": True, "roles": ["user"]},
            token_roles=["admin"],
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "admin_required"


def test_inactive_admin_is_denied(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _run_require_admin(
            monkeypatch,
            db_row={"is_active": False, "roles": ["admin"]},
            token_roles=["admin"],
        )
    assert exc.value.status_code == 403


def test_missing_user_is_denied(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _run_require_admin(monkeypatch, db_row=None, token_roles=["admin"])
    assert exc.value.status_code == 403


def test_admin_routes_are_registered_with_explicit_role_mutations():
    app = create_app()
    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
    assert ("/api/admin/context", "GET") in routes
    assert ("/api/admin/users", "GET") in routes
    assert ("/api/admin/users/{user_id}", "PATCH") in routes
    assert ("/api/admin/users/{user_id}/roles/admin", "PUT") in routes
    assert ("/api/admin/users/{user_id}/roles/admin", "DELETE") in routes
