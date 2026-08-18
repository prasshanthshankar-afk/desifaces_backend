from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from services.shared.df_contracts.v3.domain import (
    GenerationJob,
    JobState,
    MediaKind,
    MediaRole,
)
from services.shared.df_contracts.v3.face_adapter import (
    FaceMode,
    adapt_face_generate_request,
    face_job_to_compatibility_response,
    make_face_source_media_asset,
    normalize_face_parameters,
)


def test_normalize_face_parameters_collapses_current_aliases() -> None:
    params = normalize_face_parameters(
        {
            "mode": "text-to-image",
            "language": "en",
            "use_case": "social_post",
            "style": "editorial",
            "context": "festival",
            "shot_type": "medium_shot",
            "aspect_ratio": "portrait",
            "gender": "female",
            "subject_composition_code": "single_person",
            "num_variants": 2,
            "user_prompt": "Warm cinematic portrait",
        }
    )

    assert params.mode is FaceMode.TEXT_TO_IMAGE
    assert params.use_case_code == "social_post"
    assert params.style_code == "editorial"
    assert params.context_code == "festival"
    assert params.shot_type_code == "medium_shot"
    assert params.aspect_ratio == "9:16"
    assert params.subjects[0].gender == "female"
    assert params.num_variants == 2


def test_flat_and_wrapped_face_requests_produce_same_canonical_parameters() -> None:
    account_id = uuid4()
    user_id = uuid4()
    requested_at = datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc)
    studio_input = {
        "mode": "image-to-image",
        "language": "en",
        "use_case_code": "profile",
        "aspect_ratio": "1:1",
        "num_variants": 1,
        "source_image_asset_id": "legacy-face-asset",
        "preservation_strength": 0.995,
        "identity_lock": True,
    }

    flat = adapt_face_generate_request(
        studio_input,
        account_id=account_id,
        user_id=user_id,
        requested_at=requested_at,
    )
    wrapped = adapt_face_generate_request(
        {"studio": "face", "studio_input": studio_input},
        account_id=account_id,
        user_id=user_id,
        requested_at=requested_at,
    )

    assert flat.parameters == wrapped.parameters
    assert flat.generation_request.parameters == wrapped.generation_request.parameters
    assert flat.request_context.idempotency_key == wrapped.request_context.idempotency_key


def test_face_adapter_uses_resolved_media_identity_not_raw_transport_reference() -> None:
    account_id = uuid4()
    user_id = uuid4()
    source_media_id = uuid4()

    result = adapt_face_generate_request(
        {
            "mode": "image-to-image",
            "source_image_asset_id": "opaque-v2-asset-id",
            "source_image_url": "https://example.invalid/input.jpg",
            "image_size_hint": "1024x1536",
        },
        account_id=account_id,
        user_id=user_id,
        resolved_source_media_id=source_media_id,
    )

    assert result.generation_request.source_media_ids == (source_media_id,)
    assert "source_image_asset_id" not in result.generation_request.parameters
    assert "source_image_url" not in result.generation_request.parameters
    assert "image_size_hint" not in result.generation_request.parameters
    assert result.compatibility_metadata["source"]["legacy_asset_id"] == "opaque-v2-asset-id"
    assert result.compatibility_metadata["provider_hints"]["image_size_hint"] == "1024x1536"


def test_non_uuid_current_quote_id_is_preserved_without_inventing_canonical_uuid() -> None:
    result = adapt_face_generate_request(
        {
            "studio": "face",
            "studio_input": {"mode": "text-to-image", "num_variants": 2},
            "pricing_confirmation": {
                "quote_id": "qt_1234567890abcdef",
                "preview_fingerprint": "fp-123456789",
            },
        },
        account_id=uuid4(),
        user_id=uuid4(),
    )

    assert result.generation_request.pricing_quote_id is None
    assert result.pricing_confirmation is not None
    assert result.pricing_confirmation.quote_id == "qt_1234567890abcdef"
    assert result.compatibility_metadata["legacy_pricing"]["quote_id"] == "qt_1234567890abcdef"


def test_uuid_quote_id_populates_canonical_pricing_quote_reference() -> None:
    quote_id = uuid4()
    result = adapt_face_generate_request(
        {
            "studio_input": {"mode": "text-to-image"},
            "pricing_confirmation": {
                "quote_id": str(quote_id),
                "preview_fingerprint": "fingerprint-123",
            },
        },
        account_id=uuid4(),
        user_id=uuid4(),
    )

    assert result.generation_request.pricing_quote_id == quote_id
    assert "legacy_pricing" not in result.compatibility_metadata


def test_make_face_source_media_asset_creates_canonical_source_image() -> None:
    media = make_face_source_media_asset(
        account_id=uuid4(),
        owner_user_id=uuid4(),
        storage_uri="az://face-input-v3/source.jpg",
        mime_type="image/jpeg",
        metadata={"legacy_asset_id": "abc"},
    )

    assert media.kind is MediaKind.IMAGE
    assert media.role is MediaRole.SOURCE
    assert media.storage_uri == "az://face-input-v3/source.jpg"
    assert media.metadata["legacy_asset_id"] == "abc"


def test_face_job_compatibility_response_maps_canonical_state_and_media() -> None:
    now = datetime.now(timezone.utc)
    generation_id = uuid4()
    job = GenerationJob(
        generation_id=generation_id,
        state=JobState.SUCCEEDED,
        progress_percent=100,
        created_at=now,
        updated_at=now,
    )
    media = make_face_source_media_asset(
        account_id=uuid4(),
        owner_user_id=uuid4(),
        storage_uri="az://face-output-v3/result.jpg",
        metadata={
            "variant_number": 1,
            "face_profile_id": "profile-123",
            "prompt_used": "canonical prompt",
            "technical_specs": {"aspect_ratio": "9:16"},
        },
    ).model_copy(update={"role": MediaRole.FINAL, "parent_job_id": job.job_id})

    response = face_job_to_compatibility_response(
        job,
        output_media=(media,),
        public_urls={media.media_id: "https://cdn.example/result.jpg"},
    )

    assert response["job_id"] == str(job.job_id)
    assert response["status"] == "succeeded"
    assert response["progress"] == {"percent": 100}
    assert response["variants"][0]["face_profile_id"] == "profile-123"
    assert response["variants"][0]["image_url"] == "https://cdn.example/result.jpg"
    assert response["variants"][0]["media_asset_id"] == str(media.media_id)


def test_blocked_and_canceled_states_preserve_current_face_status_vocabulary() -> None:
    now = datetime.now(timezone.utc)
    blocked = GenerationJob(
        generation_id=uuid4(),
        state=JobState.BLOCKED,
        error_message="content safety blocked",
        created_at=now,
        updated_at=now,
    )
    canceled = GenerationJob(
        generation_id=uuid4(),
        state=JobState.CANCELED,
        created_at=now,
        updated_at=now,
    )

    assert face_job_to_compatibility_response(blocked)["status"] == "failed"
    assert face_job_to_compatibility_response(blocked)["error"] == "content safety blocked"
    assert face_job_to_compatibility_response(canceled)["status"] == "cancelled"
