"""Preserve authoritative speech and provide a quiet ending for web talking video."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Mapping


def should_finish_single_person(tags: Mapping[str, Any], segment_count: int) -> bool:
    # Deliberately exclude Director, mobile, cinematic and multi-segment paths.
    return (
        segment_count == 1
        and tags.get("source_surface") == "web_video_studio"
        and tags.get("audio_is_authoritative") is True
        and tags.get("longform_profile") == "talking_video"
        and tags.get("api_mode") == "legacy"
        and not any(tags.get(key) for key in ("workflow_id", "story_id", "conversation_kind", "conversation_mode"))
    )


def _duration(path: str, stream: str) -> float:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", "stream=duration:format=duration", "-of", "json", path,
    ], capture_output=True, text=True, check=True, timeout=30)
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"TALKING_FINISH_MISSING_STREAM {stream}")
    raw = streams[0].get("duration") or data.get("format", {}).get("duration")
    duration = float(raw)
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("TALKING_FINISH_INVALID_DURATION")
    return duration


def finish_single_person_video(video_path: str, audio_path: str, output_path: str) -> dict[str, float]:
    """Restore the exact input speech, then hold the last frame over silence.

    Small codec/frame rounding can be repaired locally. A materially short render
    must fail rather than disguising missing performance with a frozen frame.
    This adds no provider calls or billable generation time.
    """
    video_duration = _duration(video_path, "v:0")
    audio_duration = _duration(audio_path, "a:0")
    if audio_duration - video_duration > 0.25:
        raise RuntimeError("TALKING_FINISH_RENDER_TOO_SHORT")
    if video_duration - audio_duration > 1.0:
        raise RuntimeError("TALKING_FINISH_AUDIO_ALIGNMENT_MISMATCH")
    closing_pause = 0.75
    output_duration = max(video_duration, audio_duration) + closing_pause
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    # No audio fade, speed change, speech trimming, or use of provider audio.
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path, "-i", audio_path,
        "-filter_complex",
        f"[0:v:0]setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={output_duration-video_duration:.9f}[v];"
        "[1:a:0]asetpts=PTS-STARTPTS,apad[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{output_duration:.9f}",
        "-c:v", "libx264", "-preset", "superfast", "-crf", "18",
        "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", output_path,
    ], capture_output=True, text=True, check=True, timeout=300)
    final_video = _duration(output_path, "v:0")
    final_audio = _duration(output_path, "a:0")
    if min(final_video, final_audio) < audio_duration + closing_pause - 0.05:
        raise RuntimeError("TALKING_FINISH_CLOSING_PAUSE_MISSING")
    return {
        "source_audio_duration_sec": audio_duration,
        "rendered_video_duration_sec": video_duration,
        "closing_pause_sec": closing_pause,
        "final_video_duration_sec": final_video,
        "final_audio_duration_sec": final_audio,
    }
