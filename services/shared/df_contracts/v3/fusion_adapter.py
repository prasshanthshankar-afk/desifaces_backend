"""Fusion compatibility adapter for desifaces-v3.

The adapter is deliberately pure: it has no FastAPI, database, storage,
provider, or svc-fusion imports. Current mobile/backend aliases are collapsed at
the compatibility boundary before a canonical ``GenerationRequest(kind=fusion)``
is constructed.

Legacy artifact UUIDs and transport URLs are *not* treated as canonical
``MediaAsset`` identity. The service layer may pass already-resolved canonical
media IDs separately after ownership/lineage resolution.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence
from uuid import UUID

from pydantic import Field

from .adapters import derive_idempotency_key, make_request_context
from .common import ActorType, RequestContext, V3ContractModel
from .domain import (
    GenerationJob,
    GenerationKind,
    GenerationRequest,
    JobState,
    MediaAsset,
    MediaKind,
    MediaRole,
    ProviderExecution,
    SafetyState,
)


class FusionVoiceMode(StrEnum):
    AUDIO = "audio"
    TTS = "tts"


class FusionPricingConfirmationCompat(V3ContractModel):
    quote_id: str | None = None
    preview_fingerprint: str | None = None
    user_confirmed: bool = True


class FusionVoiceParameters(V3ContractModel):
    """Normalized current voice selection for Fusion compatibility.

    Existing inline TTS support remains represented here during C3. Longer term,
    Director/orchestration may materialize an Audio generation first and pass its
    canonical media identity to Fusion.
    """

    mode: FusionVoiceMode = FusionVoiceMode.AUDIO
    tts_voice_id: str | None = Field(default=None, max_length=500)
    tts_script: str | None = Field(default=None, max_length=4000)
    tts_language: str | None = Field(default=None, max_length=64)
    audio_locale: str | None = Field(default=None, max_length=64)
    voice_gender: str | None = Field(default=None, max_length=50)


class FusionVideoParameters(V3ContractModel):
    aspect_ratio: str = Field(default="9:16", max_length=20)
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    duration_sec: float | None = Field(default=None, gt=0, le=3600)
    resolution: str | None = Field(default=None, max_length=30)
    delivery_surface: str | None = Field(default=None, max_length=100)
    shot_type: str | None = Field(default=None, max_length=100)
    emotion: str | None = Field(default=None, max_length=100)
    motion_style: str | None = Field(default=None, max_length=100)

    profile: str | None = Field(default=None, max_length=100)
    video_mode: str | None = Field(default=None, max_length=100)
    camera_angle: str | None = Field(default=None, max_length=100)
    camera_framing: str | None = Field(default=None, max_length=100)
    camera_motion_style: str | None = Field(default=None, max_length=100)

    background_mode: str | None = Field(default=None, max_length=100)
    output_profile: str | None = Field(default=None, max_length=100)
    quality_tier: str | None = Field(default=None, max_length=100)
    video_type: str | None = Field(default=None, max_length=100)

    prompt: str | None = Field(default=None, max_length=4000)
    performance_prompt: str | None = Field(default=None, max_length=4000)
    motion_prompt: str | None = Field(default=None, max_length=4000)
    gesture_prompt: str | None = Field(default=None, max_length=4000)
    body_motion_prompt: str | None = Field(default=None, max_length=4000)
    emotion_prompt: str | None = Field(default=None, max_length=4000)
    expression_prompt: str | None = Field(default=None, max_length=4000)


class FusionGenerationParameters(V3ContractModel):
    voice: FusionVoiceParameters = Field(default_factory=FusionVoiceParameters)
    video: FusionVideoParameters = Field(default_factory=FusionVideoParameters)
    external_provider_consent: bool = False
    goal: str | None = Field(default=None, max_length=1000)
    title: str | None = Field(default=None, max_length=500)
    scenario_name: str | None = Field(default=None, max_length=500)


class FusionGenerateAdapterResult(V3ContractModel):
    request_context: RequestContext
    generation_request: GenerationRequest
    parameters: FusionGenerationParameters
    pricing_confirmation: FusionPricingConfirmationCompat | None = None
    compatibility_metadata: dict[str, Any] = Field(default_factory=dict)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _as_uuid(value: Any) -> UUID | None:
    token = _clean(value)
    if not token:
        return None
    try:
        return UUID(token)
    except (ValueError, TypeError, AttributeError):
        return None


def _duration_seconds(payload: Mapping[str, Any], video: Mapping[str, Any]) -> float | None:
    intent = _mapping(payload.get("intent"))
    raw = _first(
        video.get("duration_sec"),
        video.get("requested_duration_sec"),
        video.get("video_duration_sec"),
        video.get("pricing_duration_sec"),
        payload.get("requested_duration_sec"),
        payload.get("video_duration_sec"),
        payload.get("pricing_duration_sec"),
        payload.get("duration_sec"),
        intent.get("duration_sec"),
    )
    if raw is not None:
        try:
            value = float(raw)
            return value if value > 0 else None
        except (TypeError, ValueError):
            pass

    duration_ms = _first(video.get("duration_ms"), payload.get("duration_ms"))
    if duration_ms is not None:
        try:
            value = float(duration_ms) / 1000.0
            return value if value > 0 else None
        except (TypeError, ValueError):
            pass

    minutes = payload.get("minutes")
    if minutes is not None:
        try:
            value = float(minutes) * 60.0
            return value if value > 0 else None
        except (TypeError, ValueError):
            pass
    return None


def _dimension(video: Mapping[str, Any]) -> tuple[int | None, int | None]:
    dim = _mapping(video.get("dimension"))
    try:
        width = int(dim["width"]) if dim.get("width") is not None else None
    except (TypeError, ValueError):
        width = None
    try:
        height = int(dim["height"]) if dim.get("height") is not None else None
    except (TypeError, ValueError):
        height = None
    return width, height


def _face_reference(payload: Mapping[str, Any]) -> dict[str, Any]:
    tags = _mapping(payload.get("tags"))
    assets = _mapping(payload.get("assets"))
    provider_options = _mapping(payload.get("provider_options"))
    return {
        "url": _clean(
            _first(
                payload.get("face_image_url"),
                payload.get("image_url"),
                provider_options.get("start_image_url"),
                provider_options.get("image_url"),
            )
        ),
        "artifact_id": _clean(
            _first(
                payload.get("face_artifact_id"),
                payload.get("faceArtifactId"),
                tags.get("face_artifact_id"),
                tags.get("faceArtifactId"),
                tags.get("selected_face_artifact_id"),
                tags.get("selectedFaceArtifactId"),
                tags.get("fusion_face_artifact_id"),
                tags.get("fusionFaceArtifactId"),
                assets.get("face_artifact_id"),
                assets.get("faceArtifactId"),
            )
        ),
        "heygen_talking_photo_id": _clean(payload.get("heygen_talking_photo_id")),
        "image_key": _clean(payload.get("image_key")),
    }


def _audio_reference(payload: Mapping[str, Any]) -> dict[str, Any]:
    voice_audio = _mapping(payload.get("voice_audio"))
    assets = _mapping(payload.get("assets"))
    return {
        "url": _clean(_first(voice_audio.get("audio_url"), payload.get("audio_url"))),
        "asset_id": _clean(_first(voice_audio.get("audio_asset_id"), payload.get("audio_asset_id"))),
        "artifact_id": _clean(
            _first(
                voice_audio.get("audio_artifact_id"),
                payload.get("audio_artifact_id"),
                assets.get("voice_audio_artifact_id"),
            )
        ),
    }


def _pricing(payload: Mapping[str, Any]) -> FusionPricingConfirmationCompat | None:
    raw = _mapping(payload.get("pricing_confirmation"))
    quote_id = _clean(raw.get("quote_id"))
    fingerprint = _clean(raw.get("preview_fingerprint"))
    if quote_id is None and fingerprint is None:
        return None
    return FusionPricingConfirmationCompat(
        quote_id=quote_id,
        preview_fingerprint=fingerprint,
        user_confirmed=bool(raw.get("user_confirmed", True)),
    )


def normalize_fusion_parameters(payload: Mapping[str, Any]) -> FusionGenerationParameters:
    """Collapse direct/mobile Fusion aliases into one provider-neutral model."""

    video = _mapping(payload.get("video"))
    voice_tts = _mapping(payload.get("voice_tts"))
    consent = _mapping(payload.get("consent"))
    intent = _mapping(payload.get("intent"))
    width, height = _dimension(video)

    voice_mode = (_clean(payload.get("voice_mode")) or "audio").lower()
    if voice_mode not in {"audio", "tts"}:
        voice_mode = "audio"

    normalized_voice = FusionVoiceParameters(
        mode=voice_mode,
        tts_voice_id=_clean(_first(voice_tts.get("voice_id"), payload.get("audio_voice"))),
        tts_script=_clean(_first(voice_tts.get("script"), payload.get("script_text"))),
        tts_language=_clean(_first(voice_tts.get("language"), payload.get("audio_locale"))),
        audio_locale=_clean(payload.get("audio_locale")),
        voice_gender=_clean(payload.get("voice_gender")),
    )

    primary_prompt = _clean(
        _first(
            video.get("prompt"),
            payload.get("prompt"),
            payload.get("user_prompt"),
            payload.get("video_prompt"),
            payload.get("creative_direction"),
        )
    )

    normalized_video = FusionVideoParameters(
        aspect_ratio=_clean(video.get("aspect_ratio")) or "9:16",
        width=width,
        height=height,
        duration_sec=_duration_seconds(payload, video),
        resolution=_clean(video.get("resolution")),
        delivery_surface=_clean(video.get("delivery_surface")),
        shot_type=_clean(video.get("shot_type")),
        emotion=_clean(video.get("emotion")),
        motion_style=_clean(video.get("motion_style")),
        profile=_clean(_first(video.get("profile"), payload.get("profile"), payload.get("profile_code"))),
        video_mode=_clean(
            _first(
                video.get("video_mode"),
                payload.get("video_mode"),
                payload.get("generation_mode"),
                payload.get("product_code"),
            )
        ),
        camera_angle=_clean(_first(video.get("camera_angle"), payload.get("camera_angle"))),
        camera_framing=_clean(_first(video.get("camera_framing"), payload.get("camera_framing"))),
        camera_motion_style=_clean(
            _first(video.get("camera_motion_style"), payload.get("camera_motion_style"))
        ),
        background_mode=_clean(payload.get("background_mode")),
        output_profile=_clean(payload.get("output_profile")),
        quality_tier=_clean(payload.get("quality_tier")),
        video_type=_clean(payload.get("video_type")),
        prompt=primary_prompt,
        performance_prompt=_clean(payload.get("performance_prompt")),
        motion_prompt=_clean(_first(payload.get("motion_prompt"), payload.get("movement_prompt"))),
        gesture_prompt=_clean(payload.get("gesture_prompt")),
        body_motion_prompt=_clean(payload.get("body_motion_prompt")),
        emotion_prompt=_clean(payload.get("emotion_prompt")),
        expression_prompt=_clean(payload.get("expression_prompt")),
    )

    return FusionGenerationParameters(
        voice=normalized_voice,
        video=normalized_video,
        external_provider_consent=bool(
            _first(consent.get("external_provider_ok"), payload.get("external_provider_ok"), False)
        ),
        goal=_clean(_first(payload.get("goal"), intent.get("goal"))),
        title=_clean(payload.get("title")),
        scenario_name=_clean(payload.get("scenario_name")),
    )


def adapt_fusion_generate_request(
    payload: Mapping[str, Any],
    *,
    account_id: UUID,
    user_id: UUID,
    project_id: UUID | None = None,
    participant_ids: Sequence[UUID] = (),
    resolved_source_media_ids: Sequence[UUID] = (),
    request_id: UUID | None = None,
    correlation_id: UUID | None = None,
    explicit_idempotency_key: str | None = None,
    client_app: str | None = None,
    client_version: str | None = None,
    requested_at: datetime | None = None,
) -> FusionGenerateAdapterResult:
    """Translate a current Fusion payload into canonical V3 contracts."""

    parameters = normalize_fusion_parameters(payload)
    pricing = _pricing(payload)
    face_ref = _face_reference(payload)
    audio_ref = _audio_reference(payload)
    provider_options = dict(_mapping(payload.get("provider_options")))
    tags = dict(_mapping(payload.get("tags")))

    canonical_payload = parameters.model_dump(mode="json", exclude_none=True)
    idempotency_material = {
        "parameters": canonical_payload,
        "source_media_ids": tuple(str(item) for item in resolved_source_media_ids),
        "face_reference": face_ref,
        "audio_reference": audio_ref,
        "pricing_confirmation": (
            pricing.model_dump(mode="json", exclude_none=True) if pricing else None
        ),
    }
    idempotency_key = derive_idempotency_key(
        scope="fusion.generate",
        actor_id=user_id,
        payload=idempotency_material,
        explicit_key=explicit_idempotency_key,
    )

    effective_requested_at = requested_at or datetime.now(timezone.utc)
    context = make_request_context(
        actor_id=user_id,
        actor_type=ActorType.USER,
        account_id=account_id,
        roles=("creator",),
        scopes=("fusion:generate",),
        request_id=request_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        client_app=client_app,
        client_version=client_version,
        requested_at=effective_requested_at,
    )

    pricing_quote_id = _as_uuid(pricing.quote_id) if pricing and pricing.quote_id else None
    generation = GenerationRequest(
        account_id=account_id,
        requested_by_user_id=user_id,
        project_id=project_id,
        kind=GenerationKind.FUSION,
        participant_ids=tuple(participant_ids),
        source_media_ids=tuple(resolved_source_media_ids),
        parameters=canonical_payload,
        pricing_quote_id=pricing_quote_id,
        safety_state=SafetyState.PENDING,
        created_at=effective_requested_at,
    )

    compatibility_metadata: dict[str, Any] = {
        "source_references": {
            "face": face_ref,
            "audio": audio_ref,
            "reference_image_urls": list(payload.get("reference_image_urls") or ()),
            "reference_image_artifact_ids": list(payload.get("reference_image_artifact_ids") or ()),
        }
    }

    provider = _clean(payload.get("provider"))
    provider_hint = _clean(payload.get("provider_hint"))
    if provider or provider_hint or provider_options:
        compatibility_metadata["provider_hints"] = {
            "provider": provider,
            "provider_hint": provider_hint,
            "provider_options": provider_options,
        }

    internal_markers = {
        key: payload.get(key)
        for key in (
            "pricing_suppressed",
            "suppress_pricing",
            "skip_pricing",
            "disable_pricing",
            "internal_job",
            "child_job",
            "is_internal_child",
            "child_job_of_billable_longform_parent",
            "bill_to_parent",
        )
        if payload.get(key) is not None
    }
    billing_context = dict(_mapping(payload.get("billing_context")))
    pricing_context = dict(_mapping(payload.get("pricing_context")))
    if internal_markers or billing_context or pricing_context:
        compatibility_metadata["internal_orchestration"] = {
            "markers": internal_markers,
            "billing_context": billing_context,
            "pricing_context": pricing_context,
        }

    if tags:
        compatibility_metadata["tags"] = tags
    if pricing and pricing.quote_id and pricing_quote_id is None:
        compatibility_metadata["legacy_pricing"] = pricing.model_dump(
            mode="json", exclude_none=True
        )

    return FusionGenerateAdapterResult(
        request_context=context,
        generation_request=generation,
        parameters=parameters,
        pricing_confirmation=pricing,
        compatibility_metadata=compatibility_metadata,
    )


def make_fusion_video_media_asset(
    *,
    account_id: UUID,
    owner_user_id: UUID,
    storage_uri: str,
    mime_type: str | None = None,
    media_id: UUID | None = None,
    project_id: UUID | None = None,
    source_media_ids: Sequence[UUID] = (),
    parent_job_id: UUID | None = None,
    role: MediaRole = MediaRole.FINAL,
    metadata: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> MediaAsset:
    values: dict[str, Any] = {
        "account_id": account_id,
        "owner_user_id": owner_user_id,
        "project_id": project_id,
        "kind": MediaKind.VIDEO,
        "role": role,
        "mime_type": mime_type,
        "storage_uri": storage_uri,
        "source_media_ids": tuple(source_media_ids),
        "parent_job_id": parent_job_id,
        "metadata": dict(metadata or {}),
        "created_at": created_at or datetime.now(timezone.utc),
    }
    if media_id is not None:
        values["media_id"] = media_id
    return MediaAsset(**values)


def _compat_status(state: JobState) -> str:
    if state in {JobState.SUBMITTED, JobState.QUEUED}:
        return "queued"
    if state is JobState.RUNNING:
        return "running"
    if state is JobState.SUCCEEDED:
        return "succeeded"
    if state is JobState.CANCELED:
        return "canceled"
    return "failed"


def fusion_job_to_compatibility_response(
    job: GenerationJob,
    *,
    provider_execution: ProviderExecution | None = None,
    output_media: Sequence[MediaAsset] = (),
    public_urls: Mapping[UUID, str] | None = None,
    pricing: Mapping[str, Any] | None = None,
    pricing_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map canonical Fusion state to the current high-level job view shape."""

    url_map = public_urls or {}
    artifacts: list[dict[str, Any]] = []
    for media in output_media:
        if media.kind is not MediaKind.VIDEO:
            continue
        artifacts.append(
            {
                "kind": "video",
                "url": url_map.get(media.media_id, media.storage_uri),
                "content_type": media.mime_type,
            }
        )

    response: dict[str, Any] = {
        "job_id": str(job.job_id),
        "status": _compat_status(job.state),
        "provider": provider_execution.provider if provider_execution else None,
        "provider_job_id": (
            provider_execution.provider_request_id if provider_execution else None
        ),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "steps": [],
        "artifacts": artifacts,
    }
    if pricing is not None:
        response["pricing"] = dict(pricing)
    if pricing_summary is not None:
        response["pricing_summary"] = dict(pricing_summary)
    return response


__all__ = [
    "FusionGenerateAdapterResult",
    "FusionGenerationParameters",
    "FusionPricingConfirmationCompat",
    "FusionVideoParameters",
    "FusionVoiceMode",
    "FusionVoiceParameters",
    "adapt_fusion_generate_request",
    "fusion_job_to_compatibility_response",
    "make_fusion_video_media_asset",
    "normalize_fusion_parameters",
]
