from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict

from desifaces_shared.pricing.multi_person import AUDIO_MULTI_PERSON, participant_count

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


def _multi_person_meta(meta: Any, *, count: int, units: Any = None) -> Dict[str, Any]:
    out = dict(meta or {})
    if units is not None:
        out["chars_1k"] = str(units)
    out.update(
        {
            "multi_person": True,
            "premium": True,
            "participant_count": int(count),
            "participant_count_in_sku": False,
            "participant_scaling": "aggregate_natural_usage",
            "pricing_policy": "multi_person_workload_v1",
        }
    )
    return out


def install_multi_person_pricing_policy() -> None:
    """
    Install request-scoped Audio pricing routing.

    Multi-person billing is activated only by explicit structured context such as
    participant_count >= 2 / multi_person=true. It is never inferred from script text.
    A ContextVar keeps concurrent requests isolated.
    """
    from app.api.routes import tts_jobs as routes
    from app.services import tts_orchestrator as tts_module

    TTSOrchestrator = tts_module.TTSOrchestrator

    if getattr(routes, "_desifaces_multi_person_pricing_installed", False):
        return

    # Forward-compatible structured pricing context is additive. Existing clients
    # remain unaffected; the existing `context` string can also carry JSON such as
    # {"participant_count": 3}.
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
            payload["pricing_context"] = _multi_person_meta(
                payload.get("pricing_context"),
                count=count,
            )
        return payload

    routes._build_audio_payload = build_payload_wrapped

    original_init = TTSOrchestrator.__init__

    def init_wrapped(self: Any, *args: Any, **kwargs: Any) -> None:
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
            # PricingPreviewSpec intentionally exposes only sku_code + units + meta.
            # svc-pricing expands req.meta into quote params, so the native AUDIO
            # quantity parameter belongs in meta rather than invented constructor
            # fields such as variant_code/variant_params.
            units = str(kwargs.get("units") or "1")
            kwargs["sku_code"] = AUDIO_MULTI_PERSON
            kwargs["units"] = units
            kwargs["meta"] = _multi_person_meta(
                kwargs.get("meta"),
                count=count,
                units=units,
            )
        return original_preview_spec(*args, **kwargs)

    routes.PricingPreviewSpec = preview_spec_wrapped

    # create_job builds PricingReserveSpec inside TTSOrchestrator. Preserve the
    # same participant context used by preview so quote confirmation and reserve
    # cannot diverge for a multi-person request.
    original_reserve_spec = tts_module.PricingReserveSpec

    def reserve_spec_wrapped(*args: Any, **kwargs: Any):
        count = _participant_count_ctx.get()
        if count >= 2:
            units = str(kwargs.get("units") or "1")
            kwargs["sku_code"] = AUDIO_MULTI_PERSON
            kwargs["meta"] = _multi_person_meta(
                kwargs.get("meta"),
                count=count,
                units=units,
            )
        return original_reserve_spec(*args, **kwargs)

    tts_module.PricingReserveSpec = reserve_spec_wrapped
    routes._desifaces_multi_person_pricing_installed = True
