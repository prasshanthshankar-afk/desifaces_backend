from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Optional

from fastapi import Request
from app.db import get_pool
from app.services.support_audit import SupportAuditService


def audited_action(
    *,
    surface: str = "music_studio",
    action_name: str,
    include_response_keys: Optional[list[str]] = None,
):
    """
    Use on mutating endpoints so audits are guaranteed.
    Requires request.state.support_session_id set OR request header x-support-session-id.
    """
    include_response_keys = include_response_keys or []

    def _decorator(fn: Callable[..., Any]):
        @wraps(fn)
        async def _wrapped(*args, **kwargs):
            # Find Request
            req: Request | None = None
            for v in list(args) + list(kwargs.values()):
                if isinstance(v, Request):
                    req = v
                    break

            # Execute handler, log system event if failure
            try:
                result = await fn(*args, **kwargs)
            except Exception as e:
                if req is not None:
                    await _log_system(req, surface, action_name, {"error": str(e)})
                raise

            # Log action + optional response summary
            if req is not None:
                resp_summary = {}
                for k in include_response_keys:
                    if isinstance(result, dict) and k in result:
                        resp_summary[k] = result[k]

                await _log_action(req, surface, action_name, resp_summary)

            return result

        return _wrapped

    return _decorator


async def _log_action(req: Request, surface: str, action_name: str, extra: dict):
    pool = await get_pool()
    svc = SupportAuditService(pool)

    session_id = req.headers.get("x-support-session-id") or getattr(req.state, "support_session_id", None)
    user = getattr(req.state, "user", None)  # set by your auth dependency/middleware

    if not session_id or not user:
        return  # don’t break prod if UI forgot; but you should enforce in gateway later

    payload = {"action": action_name, "surface": surface, **(extra or {})}
    await svc.append_user_event(
        session_id=session_id,
        actor_user_id=user.user_id,
        kind="action",
        payload=payload,
        request_id=req.headers.get("x-request-id"),
        ip=req.client.host if req.client else None,
        user_agent=req.headers.get("user-agent"),
    )


async def _log_system(req: Request, surface: str, action_name: str, extra: dict):
    pool = await get_pool()
    svc = SupportAuditService(pool)

    session_id = req.headers.get("x-support-session-id") or getattr(req.state, "support_session_id", None)
    user = getattr(req.state, "user", None)

    if not session_id or not user:
        return

    payload = {"action": action_name, "surface": surface, **(extra or {})}
    await svc.append_user_event(
        session_id=session_id,
        actor_user_id=user.user_id,
        kind="system",
        payload=payload,
        request_id=req.headers.get("x-request-id"),
        ip=req.client.host if req.client else None,
        user_agent=req.headers.get("user-agent"),
    )