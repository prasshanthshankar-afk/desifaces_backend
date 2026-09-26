from __future__ import annotations

from app.services import stitch_service as target


def test_xfade_normalizes_provider_inputs_to_cfr(monkeypatch):
    commands: list[list[str]] = []

    monkeypatch.setattr(target, "_require_nonempty_file", lambda _path: None)
    monkeypatch.setattr(target, "_ensure_parent_dir", lambda _path: None)
    monkeypatch.setattr(target, "_probe_duration_seconds", lambda _path: 2.0)
    monkeypatch.setattr(target, "_run", lambda cmd, **_kwargs: commands.append(list(cmd)))

    target._xfade_pair(
        "left.mp4",
        "right.mp4",
        "out.mp4",
        transition_duration_sec=0.2,
    )

    assert len(commands) == 1
    command = commands[0]
    filter_complex = command[command.index("-filter_complex") + 1]

    assert "[0:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS" in filter_complex
    assert "[1:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS" in filter_complex
    assert "[v0][v1]xfade=" in filter_complex
    assert "[0:a]aresample=48000,asetpts=PTS-STARTPTS[a0]" in filter_complex
    assert "[1:a]aresample=48000,asetpts=PTS-STARTPTS[a1]" in filter_complex
