"""Face compatibility adapter for desifaces-v3.

The adapter is deliberately pure: it has no FastAPI, database, storage-provider,
or Face-service imports.  The existing Face HTTP contract can therefore remain
stable while service code translates requests and results to the canonical V3
contract vocabulary.
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


class FaceMode(StrEnum):
    TEXT_TO_IMAGE = "text-to-image"
    IMAGE_TO_IMAGE = "image-to-image"


class FaceSeedMode(StrEnum):
    AUTO = "auto"
    RANDOM = "random"
    DETERMINISTIC = "deterministic"


class FaceSubject(V3ContractModel):
    """Capability-specific subject hints retained from the current Face API."""

    gender: str | None = Field(default=None, max_length=50)
    relationship_role: str | None = Field(default=None, max_length=100)


class FaceGenerationParameters(V3ContractModel):
    """One normalized Face generation representation.

    Provider-facing aliases such as ``image_size_hint`` and transport references
    such as raw source URLs are intentionally excluded.  Those values may be
    preserved by ``FaceGenerateAdapterResult.compatibility_metadata`` while the
    canonical generation request contains only normalized capability parameters.
    """

    mode: FaceMode = FaceMode.TEXT_TO_IMAGE
    language: str = Field(default="en", min_length=1, max_length=30)

    age_range_code: str | None = Field(default=None, max_length=100)
    skin_tone_code: str | None = Field(default=None, max_length=100)
    region_code: str | None = Field(default=None, max_length=100)

    subject_composition_code: str = Field(default="single_person", max_length=100)
    gender: str | None = Field(default=None, max_length=50)
    subjects: tuple[FaceSubject, ...] = ()

    image_format_code: str | None = Field(default=None, max_length=100)
    use_case_code: str | None = Field(default=None, max_length=100)
    style_code: str | None = Field(default=None, max_length=100)
    context_code: str | None = Field(default=None, max_length=100)
    clothing_style_code: str | None = Field(default=None, max_length=100)
    platform_code: str | None = Field(default=None, max_length=100)

    shot_type_code: str | None = Field(default=None, max_length=100)
    aspect_ratio: str = Field(default="9:16", max_length=20)

    num_variants: int = Field(default=4, ge=1, le=8)
    user_prompt: str | None = Field(default=None, max_length=1500)

    seed_mode: FaceSeedMode = FaceSeedMode.AUTO
    seed: int | None = None
    request_nonce: str | None = Field(default=None, max_length=500)

    preservation_strength: float = Field(default=0.995, ge=0.0, le=1.0)
    identity_lock: bool = False
    identity_lock_level: str | None = Field(default=None, max_length=100)
    preserve_source_identity: bool = False
    preserve_source_gender: bool = False
    gender_lock_mode: str | None = Field(default=None, max_length=100)
    allowed_i2i_changes: tuple[str, ...] = ()
    forbidden_i2i_changes: tuple[str, ...] = ()
    identity_lock_instructions: str | None = Field(default=None, max_length=1500)

    facial_features: dict[str, str] = Field(default_factory=dict)
    preferred_variations: tuple[str, ...] = ()


class FacePricingConfirmationCompat(V3ContractModel):
    """Current Face pricing-confirmation shape kept at the compatibility edge."""

    quote_id: str
    preview_fingerprint: str | None = None
    user_confirmed: bool = True
    client_presented_amount: str | None = None
    client_presented_currency: str | None = None


class FaceGenerateAdapterResult(V3ContractModel):
    """Canonical handoff produced from a current Face create request."""

    request_context: RequestContext
    generation_request: GenerationRequest
    parameters: FaceGenerationParameters
    pricing_confirmation: FacePricingConfirmationCompat | None = None
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


def _normalize_aspect_ratio(value: Any) -> str:
    token = str(value or "9:16").strip().lower()
    aliases = {
        "portrait": "9:16",
        "vertical": "9:16",
        "landscape": "16:9",
        "horizontal": "16:9",
        "square": "1:1",
    }
    return aliases.get(token, token or "9:16")


def _pick(payload: Mapping[str, Any], canonical: str, *aliases: str) -> Any:
    value = payload.get(canonical)
    if value is not None and (not isinstance(value, str) or value.strip()):
        return value
    for alias in aliases:
        candidate = payload.get(alias)
        if candidate is not None and (
            not isinstance(candidate, str) or candidate.strip()
        ):
            return candidate
    return value


def _subjects(payload: Mapping[str, Any]) -> tuple[FaceSubject, ...]:
    raw = payload.get("subjects")
    result: list[FaceSubject] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for item in raw:
            if isinstance(item, Mapping):
                result.append(
                    FaceSubject(
                        gender=_clean_optional(item.get("gender")),
                        relationship_role=_clean_optional(item.get("relationship_role")),
                    )
                )

    composition = _clean_optional(payload.get("subject_composition_code")) or "single_person"
    gender = _clean_optional(payload.get("gender"))
    if not result and composition == "single_person":
        result.append(FaceSubject(gender=gender))
    elif not result and composition == "two_people":
        result.extend((FaceSubject(), FaceSubject()))
    elif composition == "two_people" and len(result) == 1:
        result.append(FaceSubject())

    return tuple(result)


def normalize_face_parameters(payload: Mapping[str, Any]) -> FaceGenerationParameters:
    """Collapse the current Face request aliases into one typed representation."""

    return FaceGenerationParameters(
        mode=_clean_optional(payload.get("mode")) or FaceMode.TEXT_TO_IMAGE,
        language=_clean_optional(payload.get("language")) or "en",
        age_range_code=_clean_optional(payload.get("age_range_code")),
        skin_tone_code=_clean_optional(payload.get("skin_tone_code")),
        region_code=_clean_optional(payload.get("region_code")),
        subject_composition_code=(
            _clean_optional(payload.get("subject_composition_code"))
            or "single_person"
        ),
        gender=_clean_optional(payload.get("gender")),
        subjects=_subjects(payload),
        image_format_code=_clean_optional(payload.get("image_format_code")),
        use_case_code=_clean_optional(_pick(payload, "use_case_code", "use_case")),
        style_code=_clean_optional(_pick(payload, "style_code", "style")),
        context_code=_clean_optional(_pick(payload, "context_code", "context")),
        clothing_style_code=_clean_optional(payload.get("clothing_style_code")),
        platform_code=_clean_optional(payload.get("platform_code")),
        shot_type_code=_clean_optional(_pick(payload, "shot_type_code", "shot_type")),
        aspect_ratio=_normalize_aspect_ratio(payload.get("aspect_ratio")),
        num_variants=int(payload.get("num_variants") or 4),
        user_prompt=_clean_optional(payload.get("user_prompt")),
        seed_mode=_clean_optional(payload.get("seed_mode")) or FaceSeedMode.AUTO,
        seed=payload.get("seed"),
        request_nonce=_clean_optional(payload.get("request_nonce")),
        preservation_strength=float(payload.get("preservation_strength", 0.995)),
        identity_lock=bool(payload.get("identity_lock", False)),
        identity_lock_level=_clean_optional(payload.get("identity_lock_level")),
        preserve_source_identity=bool(payload.get("preserve_source_identity", False)),
        preserve_source_gender=bool(payload.get("preserve_source_gender", False)),
        gender_lock_mode=_clean_optional(payload.get("gender_lock_mode")),
        allowed_i2i_changes=tuple(payload.get("allowed_i2i_changes") or ()),
        forbidden_i2i_changes=tuple(payload.get("forbidden_i2i_changes") or ()),
        identity_lock_instructions=_clean_optional(
            payload.get("identity_lock_instructions")
        ),
        facial_features=dict(payload.get("facial_features") or {}),
        preferred_variations=tuple(payload.get("preferred_variations") or ()),
    )


def _extract_current_request(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], FacePricingConfirmationCompat | None]:
    """Accept both current flat and wrapped Face create shapes."""

    if isinstance(payload.get("studio_input"), Mapping):
        face_payload = payload["studio_input"]
        pricing_raw = payload.get("pricing_confirmation")
    else:
        face_payload = payload
        pricing_raw = payload.get("pricing_confirmation")

    pricing: FacePricingConfirmationCompat | None = None
    if isinstance(pricing_raw, Mapping) and _clean_optional(pricing_raw.get("quote_id")):
        pricing = FacePricingConfirmationCompat(
            quote_id=str(pricing_raw["quote_id"]).strip(),
            preview_fingerprint=_clean_optional(pricing_raw.get("preview_fingerprint")),
            user_confirmed=bool(pricing_raw.get("user_confirmed", True)),
            client_presented_amount=_clean_optional(
                pricing_raw.get("client_presented_amount")
            ),
            client_presented_currency=_clean_optional(
                pricing_raw.get("client_presented_currency")
            ),
        )

    return face_payload, pricing


def adapt_face_generate_request(
    payload: Mapping[str, Any],
    *,
    account_id: UUID,
    user_id: UUID,
    project_id: UUID | None = None,
    participant_ids: Sequence[UUID] = (),
    resolved_source_media_id: UUID | None = None,
    request_id: UUID | None = None,
    correlation_id: UUID | None = None,
    explicit_idempotency_key: str | None = None,
    client_app: str | None = None,
    client_version: str | None = None,
    requested_at: datetime | None = None,
) -> FaceGenerateAdapterResult:
    """Translate a current Face create request into canonical V3 contracts.

    ``resolved_source_media_id`` is intentionally supplied by the service layer.
    The pure adapter never treats a raw URL or an opaque legacy asset identifier
    as canonical media identity.
    """

    face_payload, pricing = _extract_current_request(payload)
    parameters = normalize_face_parameters(face_payload)

    legacy_source_asset_id = _clean_optional(face_payload.get("source_image_asset_id"))
    legacy_source_url = _clean_optional(face_payload.get("source_image_url"))
    legacy_image_size_hint = _clean_optional(
        _pick(face_payload, "image_size_hint", "size")
    )

    canonical_payload = parameters.model_dump(mode="json", exclude_none=True)
    idempotency_material: dict[str, Any] = {
        "parameters": canonical_payload,
        "source_media_id": str(resolved_source_media_id)
        if resolved_source_media_id
        else None,
        "legacy_source_asset_id": legacy_source_asset_id,
        "legacy_source_url": legacy_source_url,
        "pricing_confirmation": (
            pricing.model_dump(mode="json", exclude_none=True) if pricing else None
        ),
    }
    idempotency_key = derive_idempotency_key(
        scope="face.generate",
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
        scopes=("face:generate",),
        request_id=request_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        client_app=client_app,
        client_version=client_version,
        requested_at=effective_requested_at,
    )

    pricing_quote_id = _as_uuid(pricing.quote_id) if pricing else None
    generation = GenerationRequest(
        account_id=account_id,
        requested_by_user_id=user_id,
        project_id=project_id,
        kind=GenerationKind.FACE,
        participant_ids=tuple(participant_ids),
        source_media_ids=(
            (resolved_source_media_id,) if resolved_source_media_id else ()
        ),
        parameters=canonical_payload,
        pricing_quote_id=pricing_quote_id,
        safety_state=SafetyState.PENDING,
        created_at=effective_requested_at,
    )

    compatibility_metadata: dict[str, Any] = {}
    if legacy_source_asset_id or legacy_source_url:
        compatibility_metadata["source"] = {
            "legacy_asset_id": legacy_source_asset_id,
            "legacy_url": legacy_source_url,
        }
    if legacy_image_size_hint:
        compatibility_metadata["provider_hints"] = {
            "image_size_hint": legacy_image_size_hint,
        }
    if pricing and pricing_quote_id is None:
        compatibility_metadata["legacy_pricing"] = pricing.model_dump(
            mode="json", exclude_none=True
        )

    return FaceGenerateAdapterResult(
        request_context=context,
        generation_request=generation,
        parameters=parameters,
        pricing_confirmation=pricing,
        compatibility_metadata=compatibility_metadata,
    )


def make_face_source_media_asset(
    *,
    account_id: UUID,
    owner_user_id: UUID,
    storage_uri: str,
    mime_type: str | None = None,
    media_id: UUID | None = None,
    project_id: UUID | None = None,
    metadata: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> MediaAsset:
    """Construct canonical source media after the service has stored an upload."""

    values: dict[str, Any] = {
        "account_id": account_id,
        "owner_user_id": owner_user_id,
        "project_id": project_id,
        "kind": MediaKind.IMAGE,
        "role": MediaRole.SOURCE,
        "mime_type": mime_type,
        "storage_uri": storage_uri,
        "metadata": dict(metadata or {}),
        "created_at": created_at or datetime.now(timezone.utc),
    }
    if media_id is not None:
        values["media_id"] = media_id
    return MediaAsset(**values)


def _face_compat_status(state: JobState) -> str:
    if state in {JobState.SUBMITTED, JobState.QUEUED}:
        return "queued"
    if state is JobState.RUNNING:
        return "running"
    if state is JobState.SUCCEEDED:
        return "succeeded"
    if state is JobState.CANCELED:
        return "cancelled"
    return "failed"


def face_job_to_compatibility_response(
    job: GenerationJob,
    *,
    output_media: Sequence[MediaAsset] = (),
    public_urls: Mapping[UUID, str] | None = None,
    message: str | None = None,
    progress: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map canonical job/media state back to the existing Face status response."""

    url_map = public_urls or {}
    variants: list[dict[str, Any]] = []
    for index, media in enumerate(output_media, start=1):
        if media.kind is not MediaKind.IMAGE:
            continue
        md = dict(media.metadata or {})
        image_url = (
            url_map.get(media.media_id)
            or _clean_optional(md.get("image_url"))
            or media.storage_uri
        )
        variants.append(
            {
                "variant_number": int(md.get("variant_number") or index),
                "face_profile_id": str(
                    md.get("face_profile_id") or media.media_id
                ),
                "media_asset_id": str(media.media_id),
                "image_url": image_url,
                "prompt_used": str(md.get("prompt_used") or ""),
                "technical_specs": dict(md.get("technical_specs") or {}),
                "creative_variations": dict(md.get("creative_variations") or {}),
            }
        )

    progress_payload: dict[str, Any] | None
    if progress is not None:
        progress_payload = dict(progress)
    elif job.progress_percent is not None:
        progress_payload = {"percent": job.progress_percent}
    else:
        progress_payload = None

    return {
        "job_id": str(job.job_id),
        "status": _face_compat_status(job.state),
        "message": message or "",
        "progress": progress_payload,
        "variants": variants or None,
        "error": job.error_message,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }
