from __future__ import annotations

from typing import Any, Dict

_INSTALLED = False


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def install_longform_pricing_confirmation_policy() -> None:
    """Preserve the browser-confirmed quote through the longform API boundary.

    LongformCreateRequest intentionally ignores unknown compatibility fields. The
    browser therefore mirrors pricing_confirmation into tags, which is a durable
    request field. This adapter restores it at the parent pricing reservation
    boundary before svc-pricing reserve() is called.

    Child svc-fusion jobs remain pricing-suppressed by the existing longform
    worker; only the parent longform job owns the quote/reservation lifecycle.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    import app.services.longform_orchestrator as orchestrator
    import app.api.routes.longform as route

    original = orchestrator.reserve_longform_pricing_for_job
    if getattr(original, "_df_longform_confirmation_policy", False):
        route.reserve_longform_pricing_for_job = original
        _INSTALLED = True
        return

    async def wrapped(conn, *, user_id: str, job_id: str, payload: Dict[str, Any]):
        normalized = dict(payload or {})
        if not _dict(normalized.get("pricing_confirmation")):
            tags = _dict(normalized.get("tags"))
            confirmation = _dict(tags.get("pricing_confirmation")) or _dict(tags.get("confirmed_quote"))
            if confirmation:
                normalized["pricing_confirmation"] = confirmation
        return await original(
            conn,
            user_id=user_id,
            job_id=job_id,
            payload=normalized,
        )

    setattr(wrapped, "_df_longform_confirmation_policy", True)
    orchestrator.reserve_longform_pricing_for_job = wrapped
    route.reserve_longform_pricing_for_job = wrapped
    _INSTALLED = True
