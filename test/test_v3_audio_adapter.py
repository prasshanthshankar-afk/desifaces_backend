from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from services.shared.df_contracts.v3.audio_adapter import (
    AudioGender,
    AudioTranslationTone,
    adapt_audio_tts_request,
    audio_job_to_compatibility_response,
    make_audio_media_asset,
    normalize_audio_parameters,
)
from services.shared.df_contracts.v3.domain import (
    GenerationJob,
    GenerationKind,
    JobState,
    MediaKind,
    MediaRole,
)


def test_normalize_audio_parameters_matches_current_alias_precedence() -> None:
    params = normalize_audio_parameters(
        {
            "text": "Namaste",
            "target_locale": "hi-IN",
            "voice": "legacy-voice",
            "voice_id": "mobile-voice",
            "speaker_gender": "female",
            "voice_gender": "female",
            "translation_tone": "formal",
            "output_format": "WAV",
        }
    )

    # Current svc-audio uses req.voice or req.voice_id.
    assert params.voice_id == "legacy-voice"
    assert params.voice_locale == "hi-IN"
    assert params.source_language == "en"
    assert params.speaker_gender is AudioGender.FEMALE
    assert params.voice_gender is AudioGender.FEMALE
    assert params.translation_tone is AudioTranslationTone.FORMAL
    assert params.output_format == "wav"


def test_mobile_voice_id_is_used_when_legacy_voice_is_absent() -> None:
    params = normalize_audio_parameters(
        {
            "text": "Hello",
            "target_locale": "en-US",
            "voice_id": "mobile-voice-id",
        }
    )

    assert params.voice_id == "mobile-voice-id"
    assert params.voice_locale == "en-US"


def test_input_language_alias_is_normalized() -> None:
    params = normalize_audio_parameters(
        {
            "text": "Bonjour",
            "target_locale": "en-US",
            "input_language": "fr",
        }
    )

    assert params.source_language == "fr"


def test_audio_adapter_builds_canonical_generation_and_context() -> None:
    user_id = uuid4()
    account_id = uuid4()
    participant_id = uuid4()
    requested_at = datetime.now(timezone.utc)

    result = adapt_audio_tts_request(
        {
            "text": "Hello world",
            "target_locale": "en-US",
            "voice_id": "voice-1",
            "translate": False,
        },
        account_id=account_id,
        user_id=user_id,
        participant_ids=(participant_id,),
        requested_at=requested_at,
        client_app="desifaces-mobile",
    )

    assert result.generation_request.account_id == account_id
    assert result.generation_request.requested_by_user_id == user_id
    assert result.generation_request.kind is GenerationKind.AUDIO
    assert result.generation_request.participant_ids == (participant_id,)
    assert result.generation_request.parameters["voice_id"] == "voice-1"
    assert result.request_context.actor.account_id == account_id
    assert result.request_context.idempotency_key.startswith("v3:audio.tts:")
    assert result.request_context.client_app == "desifaces-mobile"
    assert result.generation_request.created_at == requested_at


def test_audio_adapter_idempotency_is_stable() -> None:
    user_id = uuid4()
    account_id = uuid4()
    payload_a = {
        "text": "Same text",
        "target_locale": "en-US",
        "voice_id": "voice-1",
    }
    payload_b = {
        "voice_id": "voice-1",
        "target_locale": "en-US",
        "text": "Same text",
    }

    a = adapt_audio_tts_request(payload_a, account_id=account_id, user_id=user_id)
    b = adapt_audio_tts_request(payload_b, account_id=account_id, user_id=user_id)

    assert a.request_context.idempotency_key == b.request_context.idempotency_key


def test_audio_adapter_maps_uuid_quote_to_canonical_quote_id() -> None:
    quote_id = uuid4()
    result = adapt_audio_tts_request(
        {
            "text": "Hello",
            "target_locale": "en-US",
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


def test_audio_adapter_preserves_non_uuid_quote_without_inventing_identity() -> None:
    result = adapt_audio_tts_request(
        {
            "text": "Hello",
            "target_locale": "en-US",
            "pricing_confirmation": {
                "quote_id": "quote-current-format",
                "preview_fingerprint": "fingerprint-123",
            },
        },
        account_id=uuid4(),
        user_id=uuid4(),
    )

    assert result.generation_request.pricing_quote_id is None
    assert result.compatibility_metadata["legacy_pricing"]["quote_id"] == "quote-current-format"


def test_audio_adapter_records_voice_aliases_at_compatibility_edge() -> None:
    result = adapt_audio_tts_request(
        {
            "text": "Hello",
            "target_locale": "en-US",
            "voice": "legacy",
            "voice_id": "mobile",
        },
        account_id=uuid4(),
        user_id=uuid4(),
    )

    aliases = result.compatibility_metadata["voice_aliases"]
    assert aliases == {
        "voice": "legacy",
        "voice_id": "mobile",
        "resolved_voice_id": "legacy",
    }


def test_make_audio_media_asset_uses_canonical_audio_final_role() -> None:
    media_id = uuid4()
    account_id = uuid4()
    user_id = uuid4()
    source_id = uuid4()

    media = make_audio_media_asset(
        media_id=media_id,
        account_id=account_id,
        owner_user_id=user_id,
        storage_uri="az://audio-output-v3/example.mp3",
        mime_type="audio/mpeg",
        source_media_ids=(source_id,),
        metadata={"bytes": 1234},
    )

    assert media.media_id == media_id
    assert media.kind is MediaKind.AUDIO
    assert media.role is MediaRole.FINAL
    assert media.source_media_ids == (source_id,)


def test_audio_job_maps_to_current_status_response() -> None:
    now = datetime.now(timezone.utc)
    account_id = uuid4()
    user_id = uuid4()
    generation_id = uuid4()
    job_id = uuid4()
    media = make_audio_media_asset(
        account_id=account_id,
        owner_user_id=user_id,
        storage_uri="az://audio-output-v3/final.mp3",
        mime_type="audio/mpeg",
        parent_job_id=job_id,
        metadata={"bytes": 4321},
    )
    job = GenerationJob(
        job_id=job_id,
        generation_id=generation_id,
        state=JobState.SUCCEEDED,
        output_media_ids=(media.media_id,),
        created_at=now,
        updated_at=now,
    )

    response = audio_job_to_compatibility_response(
        job,
        output_media=(media,),
        public_urls={media.media_id: "https://example.test/final.mp3"},
        pricing={"status": "committed"},
        pricing_summary={"credits": 1},
    )

    assert response["job_id"] == str(job_id)
    assert response["status"] == "succeeded"
    assert response["variants"] == [
        {
            "audio_url": "https://example.test/final.mp3",
            "artifact_id": str(media.media_id),
            "content_type": "audio/mpeg",
            "bytes": 4321,
        }
    ]
    assert response["pricing"] == {"status": "committed"}
    assert response["pricing_summary"] == {"credits": 1}
