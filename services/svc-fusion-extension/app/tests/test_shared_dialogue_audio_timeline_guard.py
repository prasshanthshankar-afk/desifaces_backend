"""Prevent audio timeline mutations in shared-scene dialogue stitching.

This is a contract test, not a mouth-to-phoneme lip-sync certification.
"""
from pathlib import Path
from app.services import stitch_service as stitch


def test_shared_dialogue_uses_audio_untouched_and_no_overlap(monkeypatch, tmp_path):
    source_a = tmp_path / "speaker_a.mp4"
    source_b = tmp_path / "speaker_b.mp4"
    source_a.write_bytes(b"sample-a")
    source_b.write_bytes(b"sample-b")
    output = tmp_path / "result.mp4"

    normalized = []
    invocations = []

    def fake_normalize(src, dst, **kwargs):
        normalized.append((src, dst, kwargs))
        Path(dst).write_bytes(Path(src).read_bytes())

    def fake_run(args):
        invocations.append(args)
        Path(args[-1]).write_bytes(b"nonempty-result")

    monkeypatch.setattr(stitch, "normalize_segment_mp4", fake_normalize)
    monkeypatch.setattr(stitch, "_run", fake_run)
    monkeypatch.setattr(stitch, "_stitch_concurrency", lambda: 1)
    monkeypatch.setattr(stitch, "_shared_dialogue_transition_seconds", lambda: 0.1)

    stitch.stitch_videos(
        [str(source_a), str(source_b)], str(output),
        stitch_mode_override="shared_dialogue",
    )

    assert len(normalized) == 2
    assert all(item[2]["audio_edge_fade"] is False for item in normalized)
    assert all("edge_fade_override" in item[2] for item in normalized)
    assert len(invocations) == 1
    command = invocations[0]
    assert "-c" in command and command[command.index("-c")+1] == "copy"
    assert "-f" in command and command[command.index("-f")+1] == "concat"
    assert not any(
        token in command for token in ("atrim", "adelay", "atempo", "acrossfade", "afade")
    )
    assert output.exists()
