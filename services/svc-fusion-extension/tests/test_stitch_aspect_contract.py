from __future__ import annotations

from pathlib import Path

from app.services import stitch_service


def test_aspect_dimensions_landscape_is_16_9():
    assert stitch_service._aspect_dimensions("16:9") == (1920, 1080)


def test_normalize_segment_applies_target_aspect_ratio(tmp_path, monkeypatch):
    src = tmp_path / "input.mp4"
    dst = tmp_path / "output.mp4"
    src.write_bytes(b"x")

    monkeypatch.setattr(stitch_service, "_probe_has_audio", lambda _: True)
    monkeypatch.setattr(stitch_service, "_probe_duration_seconds", lambda _: 4.0)

    captured = {}

    def fake_run(cmd):
        captured["cmd"] = list(cmd)
        dst.write_bytes(b"out")

    monkeypatch.setattr(stitch_service, "_run", fake_run)

    stitch_service.normalize_segment_mp4(
        str(src),
        str(dst),
        edge_fade_override=0.0,
        aspect_ratio="16:9",
    )

    cmd = captured["cmd"]
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in vf
    assert "pad=1920:1080" in vf
    assert "setsar=1" in vf


def test_compose_timeline_forwards_selected_aspect_ratio(tmp_path, monkeypatch):
    seen = {}

    def fake_stitch(segment_files, out_mp4, *, stitch_mode_override=None, aspect_ratio=None):
        seen["aspect_ratio"] = aspect_ratio
        Path(out_mp4).write_bytes(b"out")

    monkeypatch.setattr(stitch_service, "stitch_videos", fake_stitch)
    monkeypatch.setattr(stitch_service, "_probe_duration_seconds", lambda _: 7.0)

    result = stitch_service.compose_timeline(
        ["a.mp4", "b.mp4"],
        str(tmp_path / "final.mp4"),
        job_id="job-1",
        aspect_ratio="16:9",
        overlay_meta={"stitch_mode": "concat"},
    )

    assert seen["aspect_ratio"] == "16:9"
    assert result["aspect_ratio"] == "16:9"
