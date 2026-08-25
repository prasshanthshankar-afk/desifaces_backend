from app.audio_autoconfigure_routes import (
    _choose_locale,
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


def test_explicit_gender_falls_back_to_durable_persona_gender():
    assert (
        _explicit_gender(
            {},
            {"gender_presentation": "female"},
        )
        == "female"
    )


def test_explicit_face_constraint_precedes_persona_gender():
    assert (
        _explicit_gender(
            {
                "explicit_face_constraints": {
                    "gender_presentation": "male",
                }
            },
            {"gender_presentation": "female"},
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


def test_unsupported_authored_locale_does_not_cross_region_fallback():
    catalog = [
        {"locale": "en-AU", "default_voice": "en-AU-AnnetteNeural"},
        {"locale": "en-GB", "default_voice": "en-GB-AbbiNeural"},
        {"locale": "ur-PK", "default_voice": "ur-PK-UzmaNeural"},
    ]

    locale, source = _choose_locale(
        existing_locale="",
        participant_locale="en-PK",
        authored_locales=["en-PK", "en-PK"],
        catalog=catalog,
    )

    assert locale is None
    assert source == "needs_user_choice"


def test_exact_authored_locale_remains_automatic():
    catalog = [
        {"locale": "ur-PK", "default_voice": "ur-PK-UzmaNeural"},
    ]

    locale, source = _choose_locale(
        existing_locale="",
        participant_locale="ur-PK",
        authored_locales=["ur-PK"],
        catalog=catalog,
    )

    assert locale is not None
    assert locale["locale"] == "ur-PK"
    assert source == "participant_locale"
