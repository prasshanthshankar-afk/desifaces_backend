from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("JWT_SECRET", "v3-admin-mutation-test-jwt-secret")
os.environ.setdefault("REFRESH_TOKEN_HMAC_SECRET", "v3-admin-mutation-test-refresh-secret")

from app.routes import admin as admin_routes  # noqa: E402


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _GovernanceConn:
    def __init__(
        self,
        *,
        user_id: str,
        roles: list[str],
        is_active: bool,
        active_super_admin_count: int = 1,
        forbid_active_count: bool = False,
    ):
        self.user_id = user_id
        self.roles = list(roles)
        self.is_active = is_active
        self.active_super_admin_count = active_super_admin_count
        self.forbid_active_count = forbid_active_count
        self.calls: list[tuple[str, str, tuple]] = []

    def transaction(self):
        return _Tx()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if "DELETE FROM core.user_roles" in query:
            role = "super_admin" if "super_admin" in self.roles else "admin"
            self.roles = [x for x in self.roles if x != role]
        return "OK"

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "UPDATE core.users" in query:
            self.is_active = self.is_active if args[1] is None else bool(args[1])
            return {
                "id": self.user_id,
                "email": "target@example.test",
                "full_name": "Target User",
                "tier": "pro",
                "is_active": self.is_active,
            }
        if "FROM core.users" in query:
            if "full_name" in query:
                return {
                    "id": self.user_id,
                    "email": "target@example.test",
                    "full_name": "Target User",
                    "tier": "pro",
                    "is_active": self.is_active,
                }
            return {
                "id": self.user_id,
                "email": "target@example.test",
                "is_active": self.is_active,
            }
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "SELECT r.role_key" in query:
            return [{"role_key": role} for role in self.roles]
        raise AssertionError(f"unexpected fetch query: {query}")

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "SELECT id FROM core.roles" in query:
            return 99
        if "COUNT(DISTINCT u.id)" in query:
            if self.forbid_active_count:
                raise AssertionError("active Super Admin count must not be queried for an inactive target")
            return self.active_super_admin_count
        raise AssertionError(f"unexpected fetchval query: {query}")


def _request(method: str = "PATCH") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/admin/test",
            "raw_path": b"/api/admin/test",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 4242),
            "server": ("testserver", 80),
        }
    )


def _install(monkeypatch, conn: _GovernanceConn, audits: list[dict]):
    async def fake_get_pool():
        return _Pool(conn)

    async def fake_audit_log(_conn, **kwargs):
        assert kwargs.get("strict") is True
        audits.append(kwargs)

    monkeypatch.setattr(admin_routes, "get_pool", fake_get_pool)
    monkeypatch.setattr(admin_routes, "audit_log", fake_audit_log)


@pytest.mark.parametrize(
    ("currently_active", "next_active"),
    [(True, False), (False, True)],
)
def test_ordinary_admin_cannot_change_privileged_account_status(monkeypatch, currently_active, next_active):
    target_id = str(uuid4())
    actor_id = str(uuid4())
    conn = _GovernanceConn(user_id=target_id, roles=["admin"], is_active=currently_active)
    audits: list[dict] = []
    _install(monkeypatch, conn, audits)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            admin_routes.patch_user(
                target_id,
                admin_routes.AdminUserPatch(is_active=next_active),
                _request(),
                {"sub": actor_id, "roles": ["admin"]},
            )
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "super_admin_required"
    assert not audits


def test_super_admin_cannot_deactivate_last_active_super_admin(monkeypatch):
    target_id = str(uuid4())
    actor_id = str(uuid4())
    conn = _GovernanceConn(
        user_id=target_id,
        roles=["super_admin"],
        is_active=True,
        active_super_admin_count=1,
    )
    audits: list[dict] = []
    _install(monkeypatch, conn, audits)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            admin_routes.patch_user(
                target_id,
                admin_routes.AdminUserPatch(is_active=False),
                _request(),
                {"sub": actor_id, "roles": ["super_admin"]},
            )
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "cannot_deactivate_last_active_super_admin"
    assert conn.is_active is True
    assert not audits


def test_super_admin_can_deactivate_peer_when_another_active_super_admin_remains(monkeypatch):
    target_id = str(uuid4())
    actor_id = str(uuid4())
    conn = _GovernanceConn(
        user_id=target_id,
        roles=["super_admin"],
        is_active=True,
        active_super_admin_count=2,
    )
    audits: list[dict] = []
    _install(monkeypatch, conn, audits)

    result = asyncio.run(
        admin_routes.patch_user(
            target_id,
            admin_routes.AdminUserPatch(is_active=False),
            _request(),
            {"sub": actor_id, "roles": ["super_admin"]},
        )
    )
    assert result["user"]["is_active"] is False
    assert len(audits) == 1
    assert audits[0]["action"] == "admin.user.update"
    assert conn.calls[0][0] == "execute"
    assert "pg_advisory_xact_lock" in conn.calls[0][1]


def test_inactive_super_admin_role_can_be_revoked_without_false_last_active_block(monkeypatch):
    target_id = str(uuid4())
    actor_id = str(uuid4())
    conn = _GovernanceConn(
        user_id=target_id,
        roles=["super_admin"],
        is_active=False,
        forbid_active_count=True,
    )
    audits: list[dict] = []
    _install(monkeypatch, conn, audits)

    result = asyncio.run(
        admin_routes._revoke_role(
            role_key="super_admin",
            user_id=target_id,
            request=_request("DELETE"),
            claims={"sub": actor_id, "roles": ["super_admin"]},
        )
    )
    assert result["roles"] == []
    assert len(audits) == 1
    assert audits[0]["action"] == "admin.role.revoke"


def test_privileged_role_cannot_be_granted_to_inactive_user(monkeypatch):
    target_id = str(uuid4())
    actor_id = str(uuid4())
    conn = _GovernanceConn(user_id=target_id, roles=[], is_active=False)
    audits: list[dict] = []
    _install(monkeypatch, conn, audits)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            admin_routes._grant_role(
                role_key="admin",
                user_id=target_id,
                request=_request("PUT"),
                claims={"sub": actor_id, "roles": ["super_admin"]},
            )
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "inactive_user_cannot_receive_privileged_role"
    assert not audits
