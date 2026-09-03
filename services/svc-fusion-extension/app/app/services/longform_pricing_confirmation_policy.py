from __future__ import annotations

from typing import Any, Dict

_INSTALLED = False


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def install_longform_pricing_confirmation_policy() -> None:
    """Preserve the browser-confirmed quote through the longform API boundary.

    The browser owns only the parent svc-fusion-extension request. Child
    svc-fusion jobs are created by the longform worker and remain pricing
    suppressed.

    LongformCreateRequest is compatibility-oriented and ignores unknown fields,
    so a top-level pricing_confirmation would otherwise disappear during
    model_validate()/model_dump(). Preserve it inside tags before validation,
    then restore it immediately before the parent pricing reservation call.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    import app.services.longform_orchestrator as orchestrator
    import app.api.routes.longform as route

    original_normalize = route._normalize_longform_request_body
    if not getattr(original_normalize, "_df_longform_confirmation_normalizer", False):
        def normalize_with_confirmation(raw: Any) -> Dict[str, Any]:
            raw_dict = _dict(raw)
            confirmation = _dict(raw_dict.get("pricing_confirmation"))
            body = original_normalize(raw)
            if confirmation:
                tags = _dict(body.get("tags"))
                tags["pricing_confirmation"] = confirmation
                body["tags"] = tags
            return body

        setattr(normalize_with_confirmation, "_df_longform_confirmation_normalizer", True)
        route._normalize_longform_request_body = normalize_with_confirmation

    original_reserve = orchestrator.reserve_longform_pricing_for_job
    if getattr(original_reserve, "_df_longform_confirmation_policy", False):
        route.reserve_longform_pricing_for_job = original_reserve
        _INSTALLED = True
        return

    async def reserve_with_confirmation(conn, *, user_id: str, job_id: str, payload: Dict[str, Any]):
        normalized = dict(payload or {})
        if not _dict(normalized.get("pricing_confirmation")):
            tags = _dict(normalized.get("tags"))
            confirmation = _dict(tags.get("pricing_confirmation")) or _dict(tags.get("confirmed_quote"))
            if confirmation:
                normalized["pricing_confirmation"] = confirmation
        return await original_reserve(
            conn,
            user_id=user_id,
            job_id=job_id,
            payload=normalized,
        )

    setattr(reserve_with_confirmation, "_df_longform_confirmation_policy", True)
    orchestrator.reserve_longform_pricing_for_job = reserve_with_confirmation
    route.reserve_longform_pricing_for_job = reserve_with_confirmation
    _INSTALLED = True
