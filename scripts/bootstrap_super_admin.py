#!/usr/bin/env python3
"""One-time V3 Super Admin bootstrap.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/bootstrap_super_admin.py user@example.com

This command is deliberately bootstrap-only. It refuses to run after any active
Super Admin exists; subsequent administrator governance must use the Admin
Console Access Control interface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

LOCK_KEY = 86300830


async def bootstrap(email: str) -> None:
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")

    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)

            existing = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT u.id)
                FROM core.users u
                JOIN core.user_roles ur ON ur.user_id = u.id
                JOIN core.roles r ON r.id = ur.role_id
                WHERE u.is_active = true AND r.role_key = 'super_admin'
                """
            )
            if int(existing or 0) > 0:
                raise RuntimeError(
                    "An active super_admin already exists. Use Admin > Access Control for further role management."
                )

            user = await conn.fetchrow(
                """
                SELECT id::text AS id, email, is_active
                FROM core.users
                WHERE lower(email) = lower($1)
                FOR UPDATE
                """,
                email.strip(),
            )
            if not user:
                raise RuntimeError("User not found")
            if not bool(user["is_active"]):
                raise RuntimeError("Inactive user cannot be bootstrapped as super_admin")

            role_id = await conn.fetchval(
                "SELECT id FROM core.roles WHERE role_key = 'super_admin'"
            )
            if role_id is None:
                raise RuntimeError(
                    "super_admin role is not configured; apply migrations/2026_08_30_v3_admin_super_admin_role.sql first"
                )

            before_roles = [
                row["role_key"]
                for row in await conn.fetch(
                    """
                    SELECT r.role_key
                    FROM core.user_roles ur
                    JOIN core.roles r ON r.id = ur.role_id
                    WHERE ur.user_id = $1::uuid
                    ORDER BY r.role_key
                    """,
                    user["id"],
                )
            ]

            await conn.execute(
                """
                INSERT INTO core.user_roles(user_id, role_id)
                VALUES ($1::uuid, $2)
                ON CONFLICT (user_id, role_id) DO NOTHING
                """,
                user["id"],
                role_id,
            )

            after_roles = sorted(set(before_roles) | {"super_admin"})
            await conn.execute(
                """
                INSERT INTO core.audit_log(
                    actor_user_id, action, entity_type, entity_id,
                    before_json, after_json, request_id, ip, user_agent
                )
                VALUES (
                    NULL, 'admin.super_admin.bootstrap', 'user_role', $1,
                    $2::jsonb, $3::jsonb, 'bootstrap-super-admin', NULL, 'bootstrap_super_admin.py'
                )
                """,
                user["id"],
                json.dumps({"roles": before_roles}),
                json.dumps({"roles": after_roles, "email": user["email"], "method": "controlled_bootstrap"}),
            )

        print(f"BOOTSTRAP_OK user_id={user['id']} email={user['email']} role=super_admin")
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the first V3 Super Admin")
    parser.add_argument("email", help="Existing active Core user email")
    args = parser.parse_args()
    try:
        asyncio.run(bootstrap(args.email))
    except Exception as exc:
        print(f"BOOTSTRAP_FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
