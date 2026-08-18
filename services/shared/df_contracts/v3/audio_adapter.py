"""Audio/TTS compatibility adapter for desifaces-v3.

The adapter is deliberately pure: it has no FastAPI, database, storage,
provider, or svc-audio imports. The existing Audio HTTP contract therefore
remains stable while service code translates requests and results to canonical
V3 contracts.
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
    SafetyState,
)


class AudioGender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    NEUTRAL = "neutral"
    UNSPECIFIED = "unspecified"


class AudioTranslationTone(StrEnum):
    NEUTRAL = "neutral"
    FORMAL = "formal"
    INFORMAL = "informal"


class AudioGenerationParameters(V3ContractModel):
    """One normalized TTS representation for current Audio generation.

    Current transport aliases such as ``voice`` vs. ``voice_id`` are collapsed
    here. Provider execution identity remains outside this model and belongs to
    ``ProviderExecution``.
    """

    text: str = Field(min_length=1, max_length=4000)
    target_locale: str = Field(min_length=2, max_length=20)
    source_language: str = Field(default="en", min_length=2, max_length=20)
    translate: bool = True

    voice_id: str | None = Field(default=None, max_length=500)
    voice_locale: str = Field(min_length=2, max_length=20)
    speaker_gender: AudioGender | None = None
    voice_gender: AudioGender | None = None
    translation_tone: AudioTranslationTone = AudioTranslationTone.NEUTRAL

    style: str | None = Field(default=None, max_length=200)
    style_degree: float | None = None
    rate: float | None = None
    pitch: float | None = None
    volume: float | None = None
    context: str | None = Field(default=None, max_length=2000)
    output_format: str = Field(default="mp3", min_length=1, max_length=30)


class AudioPricingConfirmationCompat(V3ContractModel):
    """Current Audio pricing-confirmation shape retained at the edge."""

    quote_id: str | None = None
    preview_fingerprint: str | None = None


class AudioGenerateAdapterResult(V3ContractModel):
    """Canonical handoff produced from the current Audio create request."""

    request_context: RequestContext
    generation_request: GenerationRequest
    parameters: AudioGenerationParameters
    pricing_confirmation: AudioPricingConfirmationCompat | None = None
    compatibility_metadata: dict[str, Any] = Field(default_factory=dict)


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _as_uuid(value: Any) -> UUID | None:
    clean = _clean_optional(value)
    if not clean:
        return None
    try:
        return UUID(clean)
    except (TypeError, ValueError, AttributeError):
        return None


def _normalize_gender(value: Any) -> AudioGender | None:
    clean = _clean_optional(value)
    if not clean:
        return None
    return AudioGender(clean.lower())


def normalize_audio_parameters(payload: Mapping[str, Any]) -> AudioGenerationParameters:
    """Collapse the current TTS aliases into one typed representation."""

    text = payload.get("text")
    if text is None:
        text = ""
    text = str(text)

    target_locale = _clean_optional(payload.get("target_locale")) or ""
    source_language = (
        _clean_optional(payload.get("source_language"))
        or _clean_optional(payload.get("input_language"))
        or "en"
    )

    # Preserve current service precedence: ``voice`` wins when both are sent;
    # mobile commonly sends ``voice_id``.
    voice = _clean_optional(payload.get("voice"))
    voice_id = _clean_optional(payload.get("voice_id"))
    canonical_voice = voice or voice_id

    voice_locale = _clean_optional(payload.get("voice_locale")) or target_locale
    output_format = (_clean_optional(payload.get("output_format")) or "mp3").lower()

    return AudioGenerationParameters(
        text=text,
        target_locale=target_locale,
        source_language=source_language,
        translate=bool(payload.get("translate", True)),
        voice_id=canonical_voice,
        voice_locale=voice_locale,
        speaker_gender=_normalize_gender(payload.get("speaker_gender")),
        voice_gender=_normalize_gender(payload.get("voice_gender")),
        translation_tone=(
            _clean_optional(payload.get("translation_tone"))
            or AudioTranslationTone.NEUTRAL
        ),
        style=_clean_optional(payload.get("style")),
        style_degree=payload.get("style_degree"),
        rate=payload.get("rate"),
        pitch=payload.get("pitch"),
        volume=payload.get("volume"),
        context=_clean_optional(payload.get("context")),
        output_format=output_format,
    )


def _extract_pricing_confirmation(
    payload: Mapping[str, Any],
) -> AudioPricingConfirmationCompat | None:
    raw = payload.get("pricing_confirmation")
    if not isinstance(raw, Mapping):
        return None

    quote_id = _clean_optional(raw.get("quote_id"))
    preview_fingerprint = _clean_optional(raw.get("preview_fingerprint"))
    if quote_id is None and preview_fingerprint is None:
        return None

    return AudioPricingConfirmationCompat(
        quote_id=quote_id,
        preview_fingerprint=preview_fingerprint,
    )


def adapt_audio_tts_request(
    payload: Mapping[str, Any],
    *,
    account_id: UUID,
    user_id: UUID,
    project_id: UUID | None = None,
    participant_ids: Sequence[UUID] = (),
    source_media_ids: Sequence[UUID] = (),
    request_id: UUID | None = None,
    correlation_id: UUID | None = None,
    explicit_idempotency_key: str | None = None,
    client_app: str | None = None,
    client_version: str | None = None,
    requested_at: datetime | None = None,
) -> AudioGenerateAdapterResult:
    """Translate the current ``POST /api/audio/tts`` payload to V3 contracts."""

    parameters = normalize_audio_parameters(payload)
    pricing = _extract_pricing_confirmation(payload)

    canonical_payload = parameters.model_dump(mode="json", exclude_none=True)
    raw_voice = _clean_optional(payload.get("voice"))
    raw_voice_id = _clean_optional(payload.get("voice_id"))

    idempotency_material: dict[str, Any] = {
        "parameters": canonical_payload,
        "source_media_ids": tuple(str(item) for item in source_media_ids),
        "pricing_confirmation": (
            pricing.model_dump(mode="json", exclude_none=True) if pricing else None
        ),
    }
    idempotency_key = derive_idempotency_key(
        scope="audio.tts",
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
        scopes=("audio:generate",),
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
        kind=GenerationKind.AUDIO,
        participant_ids=tuple(participant_ids),
        source_media_ids=tuple(source_media_ids),
        parameters=canonical_payload,
        pricing_quote_id=pricing_quote_id,
        safety_state=SafetyState.PENDING,
        created_at=effective_requested_at,
    )

    compatibility_metadata: dict[str, Any] = {}
    if raw_voice or raw_voice_id:
        compatibility_metadata["voice_aliases"] = {
            "voice": raw_voice,
            "voice_id": raw_voice_id,
            "resolved_voice_id": parameters.voice_id,
        }
    if pricing and pricing.quote_id and pricing_quote_id is None:
        compatibility_metadata["legacy_pricing"] = pricing.model_dump(
            mode="json", exclude_none=True
        )

    return AudioGenerateAdapterResult(
        request_context=context,
        generation_request=generation,
        parameters=parameters,
        pricing_confirmation=pricing,
        compatibility_metadata=compatibility_metadata,
    )


def make_audio_media_asset(
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
    """Construct a canonical Audio media asset after durable storage succeeds."""

    values: dict[str, Any] = {
        "account_id": account_id,
        "owner_user_id": owner_user_id,
        "project_id": project_id,
        "kind": MediaKind.AUDIO,
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


def _audio_compat_status(state: JobState) -> str:
    if state in {JobState.SUBMITTED, JobState.QUEUED}:
        return "queued"
    if state is JobState.RUNNING:
        return "running"
    if state is JobState.SUCCEEDED:
        return "succeeded"
    if state is JobState.CANCELED:
        return "canceled"
    return "failed"


def audio_job_to_compatibility_response(
    job: GenerationJob,
    *,
    output_media: Sequence[MediaAsset] = (),
    public_urls: Mapping[UUID, str] | None = None,
    pricing: Mapping[str, Any] | None = None,
    pricing_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map canonical job/media state back to the current Audio status shape."""

    url_map = public_urls or {}
    variants: list[dict[str, Any]] = []
    for media in output_media:
        if media.kind is not MediaKind.AUDIO:
            continue
        metadata = dict(media.metadata or {})
        variants.append(
            {
                "audio_url": url_map.get(media.media_id, media.storage_uri),
                "artifact_id": str(media.media_id),
                "content_type": media.mime_type,
                "bytes": metadata.get("bytes"),
            }
        )

    response: dict[str, Any] = {
        "job_id": str(job.job_id),
        "status": _audio_compat_status(job.state),
        "error_code": job.error_code,
        "error_message": job.error_message,
        "variants": variants,
    }
    if pricing is not None:
        response["pricing"] = dict(pricing)
    if pricing_summary is not None:
        response["pricing_summary"] = dict(pricing_summary)
    return response


__all__ = [
    "AudioGender",
    "AudioGenerateAdapterResult",
    "AudioGenerationParameters",
    "AudioPricingConfirmationCompat",
    "AudioTranslationTone",
    "adapt_audio_tts_request",
    "audio_job_to_compatibility_response",
    "make_audio_media_asset",
    "normalize_audio_parameters",
]
