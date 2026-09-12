from __future__ import annotations

from pathlib import Path

from app.services import v3_stitch_resilience as target


def test_transient_segment_timeout_retries_only_failed_segment(monkeypatch, tmp_path):
    monkeypatch.setenv("V3_SCENE_STITCH_DOWNLOAD_ATTEMPTS", "3")
    monkeypatch.setenv("V3_SCENE_STITCH_DOWNLOAD_CONCURRENCY", "1")
    monkeypatch.setenv("V3_SCENE_STITCH_DOWNLOAD_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("V3_SCENE_STITCH_DOWNLOAD_TIMEOUT_SECONDS", "300")

    calls: dict[str, int] = {}

    def fake_download(url: str, output_path: str, *, timeout_seconds: int = 120):
        calls[url] = calls.get(url, 0) + 1
        assert timeout_seconds == 300
        if url.endswith("first.mp4") and calls[url] == 1:
            raise TimeoutError("The read operation timed out")
        Path(output_path).write_bytes((url + "\n").encode("utf-8"))
        return output_path

    stitched_inputs: list[str] = []

    def fake_stitch(segment_files: list[str], out_mp4: str):
        stitched_inputs.extend(segment_files)
        assert all(Path(path).stat().st_size > 0 for path in segment_files)
        Path(out_mp4).write_bytes(b"stitched")

    monkeypatch.setattr(target, "download_to_local", fake_download)
    monkeypatch.setattr(target, "stitch_videos", fake_stitch)

    output = tmp_path / "scene.mp4"
    target.resilient_stitch_video_urls(
        [
            "https://provider.example/first.mp4",
            "https://provider.example/second.mp4",
        ],
        str(output),
    )

    assert output.read_bytes() == b"stitched"
    assert calls["https://provider.example/first.mp4"] == 2
    assert calls["https://provider.example/second.mp4"] == 1
    assert len(stitched_inputs) == 2


def test_exhausted_download_error_redacts_url_query(monkeypatch, tmp_path):
    monkeypatch.setenv("V3_SCENE_STITCH_DOWNLOAD_ATTEMPTS", "2")
    monkeypatch.setenv("V3_SCENE_STITCH_DOWNLOAD_CONCURRENCY", "1")
    monkeypatch.setenv("V3_SCENE_STITCH_DOWNLOAD_RETRY_BACKOFF_SECONDS", "0")

    def always_fail(url: str, output_path: str, *, timeout_seconds: int = 120):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(target, "download_to_local", always_fail)

    secret_url = "https://cdn.example/video.mp4?secret=do-not-log"
    output = tmp_path / "scene.mp4"

    try:
        target.resilient_stitch_video_urls([secret_url], str(output))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected exhausted download failure")

    assert "cdn.example" in message
    assert "secret=do-not-log" not in message
    assert "attempts=2" in message
