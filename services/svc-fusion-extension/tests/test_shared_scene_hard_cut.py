from __future__ import annotations

from app.services import stitch_service
from app.api.routes.v3_scene_stitch import _effective_scene_stitch_mode, _media_storage_location


def _capture_normalize_command(monkeypatch, *, edge_fade_override, audio_edge_fade=True):
    commands = []

    monkeypatch.setattr(stitch_service, "_require_nonempty_file", lambda _path: None)
    monkeypatch.setattr(stitch_service, "_probe_has_audio", lambda _path: True)
    monkeypatch.setattr(stitch_service, "_probe_duration_seconds", lambda _path: 5.0)
    monkeypatch.setattr(stitch_service, "_segment_edge_fade_seconds", lambda: 0.12)
    monkeypatch.setattr(stitch_service, "_run", lambda cmd, **_kwargs: commands.append(list(cmd)))

    stitch_service.normalize_segment_mp4(
        "input.mp4",
        "output.mp4",
        edge_fade_override=edge_fade_override,
        audio_edge_fade=audio_edge_fade,
    )
    assert commands
    return commands[-1]


def test_hard_cut_normalization_disables_turn_edge_fades(monkeypatch):
    command = _capture_normalize_command(monkeypatch, edge_fade_override=0.0)
    vf = command[command.index("-vf") + 1]
    assert "fade=t=in" not in vf
    assert "fade=t=out" not in vf


def test_default_normalization_keeps_existing_edge_fades(monkeypatch):
    command = _capture_normalize_command(monkeypatch, edge_fade_override=None)
    vf = command[command.index("-vf") + 1]
    assert "fade=t=in" in vf
    assert "fade=t=out" in vf


def test_shared_scene_forces_hard_cut_even_if_xfade_requested():
    assert _effective_scene_stitch_mode("xfade", "shared_scene") == "hard_cut"
    assert _effective_scene_stitch_mode("fade", "shared_scene") == "hard_cut"


def test_ordered_speaker_shots_preserve_requested_mode():
    assert _effective_scene_stitch_mode("xfade", "ordered_speaker_shots") == "xfade"
    assert _effective_scene_stitch_mode("concat", "ordered_speaker_shots") == "concat"


def test_shared_media_location_recovers_face_upload_storage():
    container, blob = _media_storage_location(
        "https://account.blob.core.windows.net/face-output/group/a.png?sig=expired",
        {},
    )
    assert container == "face-output"
    assert blob == "group/a.png"


def test_shared_dialogue_normalization_fades_video_only(monkeypatch):
    command = _capture_normalize_command(
        monkeypatch,
        edge_fade_override=0.12,
        audio_edge_fade=False,
    )
    vf = command[command.index("-vf") + 1]
    assert "fade=t=in" in vf
    assert "fade=t=out" in vf
    assert "-af" not in command


def test_shared_dialogue_stitch_never_uses_xfade_or_audio_crossfade(monkeypatch):
    normalizations = []
    commands = []

    monkeypatch.setattr(stitch_service, "_require_nonempty_file", lambda _path: None)
    monkeypatch.setattr(stitch_service, "_stitch_concurrency", lambda: 1)
    monkeypatch.setattr(stitch_service, "_shared_dialogue_transition_seconds", lambda: 0.12)
    monkeypatch.setattr(
        stitch_service,
        "normalize_segment_mp4",
        lambda src, dst, **kwargs: normalizations.append((src, dst, kwargs)),
    )
    monkeypatch.setattr(
        stitch_service,
        "_xfade_pair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("shared_dialogue must not use xfade/acrossfade")
        ),
    )
    monkeypatch.setattr(stitch_service, "_run", lambda cmd, **_kwargs: commands.append(list(cmd)))

    stitch_service.stitch_videos(
        ["turn-1.mp4", "turn-2.mp4"],
        "final.mp4",
        stitch_mode_override="shared_dialogue",
        aspect_ratio="16:9",
    )

    assert len(normalizations) == 2
    for _src, _dst, kwargs in normalizations:
        assert kwargs["edge_fade_override"] == 0.12
        assert kwargs["audio_edge_fade"] is False
        assert kwargs["aspect_ratio"] == "16:9"

    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("-f") + 1] == "concat"
    assert command[command.index("-c") + 1] == "copy"
    assert "acrossfade" not in " ".join(command)
