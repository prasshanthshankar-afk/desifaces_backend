from __future__ import annotations

from app.services import stitch_service


def _capture_normalize_command(monkeypatch, *, edge_fade_override):
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
