from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict

from desifaces_shared.pricing.multi_person import (
    AUDIO_MULTI_PERSON,
    audio_units_from_chars,
    participant_count,
)

_participant_count_ctx: ContextVar[int] = ContextVar(
    "desifaces_audio_pricing_participant_count",
    default=1,
)


def _request_context(req: Any) -> Dict[str, Any]:
    context: Dict[str, Any] = {}
    try:
        dumped = req.model_dump(mode="json")
        if isinstance(dumped, dict):
            context.update(dumped)
    except Exception:
        pass
    extra = getattr(req, "model_extra", None)
    if isinstance(extra, dict):
        context.update(extra)
    raw_context = getattr(req, "context", None)
    if raw_context is not None:
        context["context"] = raw_context
    return context


def install_multi_person_pricing_policy() -> None:
    """
    Install request-scoped Audio pricing routing.

    Multi-person billing is activated only by explicit structured context such as
    participant_count >= 2 / multi_person=true. It is never inferred from script text.
    A ContextVar keeps concurrent requests isolated.
    """
    from app.api.routes import tts_jobs as routes
    from app.services.tts_orchestrator import TTSOrchestrator

    if getattr(routes, "_desifaces_multi_person_pricing_installed", False):
        return

    # Accept forward-compatible structured pricing context without changing the
    # existing required TTS request fields. Existing clients remain unaffected.
    try:
        routes.TTSCreateRequest.model_config = dict(routes.TTSCreateRequest.model_config or {})
        routes.TTSCreateRequest.model_config["extra"] = "allow"
        routes.TTSCreateRequest.model_rebuild(force=True)
    except Exception:
        pass

    original_build_payload = routes._build_audio_payload

    def build_payload_wrapped(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        req = kwargs.get("req")
        if req is None:
            for value in args:
                if isinstance(value, routes.TTSCreateRequest):
                    req = value
                    break
        count = participant_count(_request_context(req)) if req is not None else 1
        _participant_count_ctx.set(count)
        payload = dict(original_build_payload(*args, **kwargs) or {})
        if count >= 2:
            pricing_context = payload.get("pricing_context")
            if not isinstance(pricing_context, dict):
                pricing_context = {}
            pricing_context.update(
                {
                    "multi_person": True,
                    "premium": True,
                    "participant_count": count,
                    "pricing_policy": "multi_person_workload_v1",
                }
            )
            payload["pricing_context"] = pricing_context
        return payload

    routes._build_audio_payload = build_payload_wrapped

    original_init = TTSOrchestrator.__init__

    def init_wrapped(self: TTSOrchestrator, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if _participant_count_ctx.get() >= 2:
            # Instance-level override: no class/global SKU mutation and therefore
            # no premium leakage into concurrent single-person requests.
            self.VARIANT_CODE = AUDIO_MULTI_PERSON

    TTSOrchestrator.__init__ = init_wrapped

    original_preview_spec = routes.PricingPreviewSpec

    def preview_spec_wrapped(*args: Any, **kwargs: Any):
        count = _participant_count_ctx.get()
        if count >= 2:
            kwargs["sku_code"] = AUDIO_MULTI_PERSON
            kwargs["variant_code"] = AUDIO_MULTI_PERSON
            params = dict(kwargs.get("variant_params") or {})
            chars = params.get("chars") or params.get("text_length") or 1
            units = audio_units_from_chars(chars)
            kwargs["units"] = str(units)
            kwargs["variant_params"] = {"chars_1k": str(units)}
            meta = dict(kwargs.get("meta") or {})
            meta.update(
                {
                    "multi_person": True,
                    "premium": True,
                    "participant_count": count,
                    "pricing_policy": "multi_person_workload_v1",
                }
            )
            kwargs["meta"] = meta
        return original_preview_spec(*args, **kwargs)

    routes.PricingPreviewSpec = preview_spec_wrapped
    routes._desifaces_multi_person_pricing_installed = True
