from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from uuid import UUID

import asyncpg


async def get_feature_flag(
    conn: asyncpg.Connection,
    *,
    flag_key: str,
    user_id: Optional[str] = None,
    tier: Optional[str] = None,
    default_enabled: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Resolve feature flags with precedence:
      1) user scope  (scope='user', scope_key=<user_id>)
      2) tier scope  (scope='tier', scope_key=<tier>)
      3) global      (scope='global', scope_key is NULL)
      4) default

    Returns: (enabled, config_json)

    NOTE: scope_key is stored as text. For user scope, pass user_id as UUID string.
    """

    params = [flag_key]
    clauses = []

    # user override
    if user_id:
        clauses.append("(scope='user' AND scope_key=$2)")
        params.append(str(user_id))

    # tier override
    if tier:
        clauses.append(f"(scope='tier' AND scope_key=${len(params)+1})")
        params.append(str(tier))

    # global
    clauses.append("(scope='global' AND scope_key IS NULL)")

    where = " OR ".join(clauses)

    row = await conn.fetchrow(
        f"""
        SELECT enabled, config_json
        FROM core.feature_flags
        WHERE flag_key=$1 AND ({where})
        ORDER BY
          CASE
            WHEN scope='user'  THEN 3
            WHEN scope='tier'  THEN 2
            WHEN scope='global' THEN 1
            ELSE 0
          END DESC
        LIMIT 1
        """,
        *params,
    )

    if not row:
        return default_enabled, {}

    cfg = row["config_json"] or {}
    return bool(row["enabled"]), dict(cfg)


async def require_feature_flag(
    conn: asyncpg.Connection,
    *,
    flag_key: str,
    user_id: Optional[str] = None,
    tier: Optional[str] = None,
    default_enabled: bool = False,
) -> Dict[str, Any]:
    """
    Convenience: raises PermissionError if disabled.
    Returns config_json if enabled.
    """
    enabled, cfg = await get_feature_flag(
        conn,
        flag_key=flag_key,
        user_id=user_id,
        tier=tier,
        default_enabled=default_enabled,
    )
    if not enabled:
        raise PermissionError(f"feature_disabled:{flag_key}")
    return cfg