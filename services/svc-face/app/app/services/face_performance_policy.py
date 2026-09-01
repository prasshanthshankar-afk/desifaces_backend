from __future__ import annotations

import os

from app.services.creator_orchestrator import CreatorOrchestrator

_INSTALLED = False


def install_face_performance_policy() -> None:
    """Raise the launch default variant concurrency from 3 to 4.

    The value remains bounded and explicitly overrideable with
    DF_FACE_VARIANT_CONCURRENCY. This lets the web's common four-variant request
    execute in a single provider wave when upstream capacity permits.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    def _face_variant_concurrency(self: CreatorOrchestrator) -> int:
        try:
            return max(1, min(8, int(os.getenv("DF_FACE_VARIANT_CONCURRENCY", "4"))))
        except Exception:
            return 4

    CreatorOrchestrator._face_variant_concurrency = _face_variant_concurrency  # type: ignore[assignment]
    _INSTALLED = True
