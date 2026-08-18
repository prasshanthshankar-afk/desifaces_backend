from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from services.shared.df_contracts.v3.domain import (
    GenerationJob,
    GenerationKind,
    JobState,
    MediaKind,
    ProviderExecution,
    ProviderExecutionState,
)
from services.shared.df_contracts.v3.fusion_adapter import (
    FusionVoiceMode,
    adapt_fusion_generate_request,
    fusion_job_to_compatibility_response,
    make_fusion_video_media_asset,
    normalize_fusion_parameters,
)


def test_normalize_fusion_collapses_mobile_aliases() -> None:
    payload = {
        "voice_mode": "audio",
        "video": {
            "aspect_ratio": "16:9",
            "requested_duration_sec": 42,
            "profile": "cinematic_video_direction",
            "video_mode": "CINEMATIC_VIDEO_DIRECTION",
            "camera_angle": "low_angle",
            "camera_framing": "medium_shot",
            "camera_motion_style": "slow_push_in",
        },
        "profile_code": "talking_video",
        "generation_mode": "TALKING_VIDEO",
        "user_prompt": "cinematic performance",
        "movement_prompt": "subtle head movement",
        "gesture_prompt": "natural hand gestures",
        "goal": "brand story",
        "consent": {"external_provider_ok": True},
    }

    params = normalize_fusion_parameters(payload)

    assert params.voice.mode is FusionVoiceMode.AUDIO
    assert params.video.aspect_ratio == "16:9"
    assert params.video.duration_sec == 42
    assert params.video.profile == "cinematic_video_direction"
    assert params.video.video_mode == "CINEMATIC_VIDEO_DIRECTION"
    assert params.video.camera_angle == "low_angle"
    assert params.video.camera_framing == "medium_shot"
    assert params.video.camera_motion_style == "slow_push_in"
    assert params.video.prompt == "cinematic performance"
    assert params.video.motion_prompt == "subtle head movement"
    assert params.video.gesture_prompt == "natural hand gestures"
    assert params.goal == "brand story"
    assert params.external_provider_consent is True


def test_duration_aliases_normalize_milliseconds_and_minutes() -> None:
    from_ms = normalize_fusion_parameters({"video": {"duration_ms": 12500}})
    assert from_ms.video.duration_sec == 12.5

    from_minutes = normalize_fusion_parameters({"minutes": 2})
    assert from_minutes.video.duration_sec == 120


def test_inline_tts_is_capability_specific_not_generic_provider_state() -> None:
    params = normalize_fusion_parameters(
        {
            "voice_mode": "tts",
            "voice_tts": {
                "voice_id": "voice-123",
                "script": "Hello from Fusion",
                "language": "en-IN",
            },
            "voice_gender": "female",
        }
    )

    assert params.voice.mode is FusionVoiceMode.TTS
    assert params.voice.tts_voice_id == "voice-123"
    assert params.voice.tts_script == "Hello from Fusion"
    assert params.voice.tts_language == "en-IN"
    assert params.voice.voice_gender == "female"


def test_adapter_keeps_legacy_sources_out_of_canonical_media_ids() -> None:
    account_id = uuid4()
    user_id = uuid4()
    face_artifact_id = uuid4()
    audio_artifact_id = uuid4()

    result = adapt_fusion_generate_request(
        {
            "face_artifact_id": str(face_artifact_id),
            "voice_audio": {"audio_artifact_id": str(audio_artifact_id)},
            "provider": "omnihuman_v15",
            "provider_hint": "premium-provider",
            "provider_options": {"model_name": "example-model"},
            "video": {"duration_sec": 10},
        },
        account_id=account_id,
        user_id=user_id,
    )

    assert result.generation_request.kind is GenerationKind.FUSION
    assert result.generation_request.source_media_ids == ()
    sources = result.compatibility_metadata["source_references"]
    assert sources["face"]["artifact_id"] == str(face_artifact_id)
    assert sources["audio"]["artifact_id"] == str(audio_artifact_id)
    provider_hints = result.compatibility_metadata["provider_hints"]
    assert provider_hints["provider"] == "omnihuman_v15"
    assert provider_hints["provider_hint"] == "premium-provider"
    assert "provider" not in result.generation_request.parameters
    assert "provider_options" not in result.generation_request.parameters


def test_adapter_uses_only_service_resolved_canonical_media_ids() -> None:
    face_media_id = uuid4()
    audio_media_id = uuid4()
    result = adapt_fusion_generate_request(
        {"video": {"duration_sec": 15}},
        account_id=uuid4(),
        user_id=uuid4(),
        resolved_source_media_ids=(face_media_id, audio_media_id),
    )

    assert result.generation_request.source_media_ids == (face_media_id, audio_media_id)


def test_pricing_uuid_maps_canonically_and_legacy_id_is_preserved() -> None:
    quote_uuid = uuid4()
    canonical = adapt_fusion_generate_request(
        {
            "pricing_confirmation": {
                "quote_id": str(quote_uuid),
                "preview_fingerprint": "fingerprint-1",
            }
        },
        account_id=uuid4(),
        user_id=uuid4(),
    )
    assert canonical.generation_request.pricing_quote_id == quote_uuid
    assert "legacy_pricing" not in canonical.compatibility_metadata

    legacy = adapt_fusion_generate_request(
        {
            "pricing_confirmation": {
                "quote_id": "fusion-quote-legacy",
                "preview_fingerprint": "fingerprint-2",
            }
        },
        account_id=uuid4(),
        user_id=uuid4(),
    )
    assert legacy.generation_request.pricing_quote_id is None
    assert legacy.compatibility_metadata["legacy_pricing"]["quote_id"] == "fusion-quote-legacy"


def test_idempotency_is_stable_across_mapping_order() -> None:
    account_id = uuid4()
    user_id = uuid4()
    a = adapt_fusion_generate_request(
        {
            "video": {"duration_sec": 20, "aspect_ratio": "9:16"},
            "voice_mode": "audio",
        },
        account_id=account_id,
        user_id=user_id,
    )
    b = adapt_fusion_generate_request(
        {
            "voice_mode": "audio",
            "video": {"aspect_ratio": "9:16", "duration_sec": 20},
        },
        account_id=account_id,
        user_id=user_id,
    )

    assert a.request_context.idempotency_key == b.request_context.idempotency_key


def test_internal_child_markers_stay_compatibility_metadata() -> None:
    result = adapt_fusion_generate_request(
        {
            "child_job": True,
            "bill_to_parent": True,
            "pricing_suppressed": True,
            "billing_context": {"parent_longform_job_id": str(uuid4())},
        },
        account_id=uuid4(),
        user_id=uuid4(),
    )

    internal = result.compatibility_metadata["internal_orchestration"]
    assert internal["markers"]["child_job"] is True
    assert internal["markers"]["bill_to_parent"] is True
    assert "child_job" not in result.generation_request.parameters


def test_video_media_and_compatibility_response_separate_provider_execution() -> None:
    now = datetime.now(timezone.utc)
    account_id = uuid4()
    user_id = uuid4()
    generation_id = uuid4()
    job_id = uuid4()
    source_media_id = uuid4()

    media = make_fusion_video_media_asset(
        account_id=account_id,
        owner_user_id=user_id,
        storage_uri="az://video-output-v3/final.mp4",
        mime_type="video/mp4",
        source_media_ids=(source_media_id,),
        parent_job_id=job_id,
        created_at=now,
    )
    assert media.kind is MediaKind.VIDEO
    assert media.source_media_ids == (source_media_id,)

    job = GenerationJob(
        job_id=job_id,
        generation_id=generation_id,
        state=JobState.SUCCEEDED,
        progress_percent=100,
        output_media_ids=(media.media_id,),
        created_at=now,
        updated_at=now,
    )
    execution = ProviderExecution(
        job_id=job_id,
        provider="omnihuman_v15",
        capability="fusion.video.generate",
        model="provider-model",
        state=ProviderExecutionState.SUCCEEDED,
        provider_request_id="provider-job-123",
        created_at=now if False else None,
    )

    response = fusion_job_to_compatibility_response(
        job,
        provider_execution=execution,
        output_media=(media,),
        public_urls={media.media_id: "https://example.test/final.mp4"},
    )

    assert response["status"] == "succeeded"
    assert response["provider"] == "omnihuman_v15"
    assert response["provider_job_id"] == "provider-job-123"
    assert response["artifacts"][0]["url"] == "https://example.test/final.mp4"
