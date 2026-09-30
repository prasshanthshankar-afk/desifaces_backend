from app.services.tts_intent_policy import (
    relax_native_style_only_for_eligible_sarvam_voice,
)


def test_non_sarvam_requests_keep_existing_style_semantics():
    assert (
        relax_native_style_only_for_eligible_sarvam_voice(
            "Conversational",
            sarvam_voice_eligible=False,
        )
        is False
    )
    assert (
        relax_native_style_only_for_eligible_sarvam_voice(
            "Narration",
            sarvam_voice_eligible=False,
        )
        is False
    )


def test_only_eligible_sarvam_conversational_and_narration_relax():
    assert (
        relax_native_style_only_for_eligible_sarvam_voice(
            "Conversational",
            sarvam_voice_eligible=True,
        )
        is True
    )
    assert (
        relax_native_style_only_for_eligible_sarvam_voice(
            "Narration",
            sarvam_voice_eligible=True,
        )
        is True
    )


def test_character_and_unknown_styles_stay_strict_even_for_sarvam():
    assert (
        relax_native_style_only_for_eligible_sarvam_voice(
            "Character",
            sarvam_voice_eligible=True,
        )
        is False
    )
    assert (
        relax_native_style_only_for_eligible_sarvam_voice(
            "cheerful",
            sarvam_voice_eligible=True,
        )
        is False
    )


def test_empty_style_never_needs_relaxation():
    assert (
        relax_native_style_only_for_eligible_sarvam_voice(
            None,
            sarvam_voice_eligible=True,
        )
        is False
    )
