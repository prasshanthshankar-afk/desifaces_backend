from app.services.subtitle_service import build_webvtt


def test_build_webvtt_concat_uses_segment_boundaries():
    body = build_webvtt(
        [
            {"duration_sec": 2, "text_chunk": "Hello there."},
            {"duration_sec": 3, "text_chunk": "Welcome to desifaces."},
        ],
        stitch_mode="concat",
    )
    assert "00:00:00.000 --> 00:00:02.000" in body
    assert "00:00:02.000 --> 00:00:05.000" in body
    assert "Hello there." in body
    assert "Welcome to desifaces." in body


def test_build_webvtt_xfade_accounts_for_overlap():
    body = build_webvtt(
        [
            {"duration_sec": 2, "text_chunk": "One"},
            {"duration_sec": 2, "text_chunk": "Two"},
        ],
        stitch_mode="xfade",
        transition_seconds=0.5,
    )
    assert "00:00:01.500 --> 00:00:03.500" in body


def test_build_webvtt_supports_speaker_label():
    body = build_webvtt(
        [{"duration_sec": 2, "text_chunk": "Hi", "speaker_name": "Maya"}]
    )
    assert "Maya: Hi" in body
