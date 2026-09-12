from __future__ import annotations

from typing import Any


def _director_face_pricing_context(studio_input: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(studio_input or {})
    pricing_context = dict(resolved.get("pricing_context") or {})
    pricing_context.update(
        {
            "multi_person": True,
            "pricing_scope": "director_participant_identity",
            "participant_count_in_sku": False,
        }
    )
    resolved["pricing_context"] = pricing_context
    return resolved


def install_director_face_pricing_context() -> None:
    """Mark Director participant Face requests as multi-person premium work.

    Director deliberately generates each cast identity as a single-person portrait.
    Image composition therefore must stay `single_person`, but pricing must still
    know that the identity belongs to the multi-person orchestration flow. This
    adapter decorates only Director's FaceStudioClient calls and does not alter
    ordinary Face Studio requests.
    """
    from .participant_face import FaceStudioClient

    original_preview = FaceStudioClient.preview_pricing
    if getattr(original_preview, "_desifaces_director_face_pricing_context", False):
        return

    original_create = FaceStudioClient.create_job

    async def preview_wrapped(
        self: FaceStudioClient,
        *,
        headers: dict[str, str],
        studio_input: dict[str, Any],
    ) -> dict[str, Any]:
        return await original_preview(
            self,
            headers=headers,
            studio_input=_director_face_pricing_context(studio_input),
        )

    async def create_wrapped(
        self: FaceStudioClient,
        *,
        headers: dict[str, str],
        studio_input: dict[str, Any],
        pricing_preview: dict[str, Any],
        request_nonce: str | None = None,
    ) -> str:
        return await original_create(
            self,
            headers=headers,
            studio_input=_director_face_pricing_context(studio_input),
            pricing_preview=pricing_preview,
            request_nonce=request_nonce,
        )

    preview_wrapped._desifaces_director_face_pricing_context = True  # type: ignore[attr-defined]
    create_wrapped._desifaces_director_face_pricing_context = True  # type: ignore[attr-defined]
    FaceStudioClient.preview_pricing = preview_wrapped
    FaceStudioClient.create_job = create_wrapped
