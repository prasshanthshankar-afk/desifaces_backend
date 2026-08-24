from app.audio_autoconfigure_routes import (
    _choose_voice,
    _explicit_gender,
)


VOICES = [
    {
        "voice_name": "en-AU-AnnetteNeural",
        "gender": "female",
        "is_default": True,
    },
    {
        "voice_name": "en-AU-WilliamNeural",
        "gender": "male",
        "is_default": False,
    },
]


def test_explicit_gender_reads_durable_face_constraint():
    assert (
        _explicit_gender(
            {
                "explicit_face_constraints": {
                    "gender": "male",
                }
            }
        )
        == "male"
    )


def test_female_existing_voice_is_preserved():
    result = _choose_voice(
        VOICES,
        preferred_voice="en-AU-AnnetteNeural",
        requested_gender="female",
    )

    assert result is not None
    assert result["voice_name"] == "en-AU-AnnetteNeural"


def test_incompatible_existing_voice_is_replaced_by_compatible_voice():
    result = _choose_voice(
        VOICES,
        preferred_voice="en-AU-AnnetteNeural",
        requested_gender="male",
    )

    assert result is not None
    assert result["voice_name"] == "en-AU-WilliamNeural"
    assert result["gender"] == "male"


def test_missing_compatible_gender_fails_closed():
    result = _choose_voice(
        [VOICES[0]],
        preferred_voice="en-AU-AnnetteNeural",
        requested_gender="male",
    )

    assert result is None
