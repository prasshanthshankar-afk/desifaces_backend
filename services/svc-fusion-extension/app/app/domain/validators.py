from __future__ import annotations

from app.domain.models import VideoIntent


def validate_video_intent(intent: VideoIntent) -> VideoIntent:
    if not intent.goal or not intent.goal.strip():
        raise ValueError("goal is required")
    if intent.duration_sec <= 0:
        raise ValueError("duration_sec must be > 0")
    if intent.constraints.max_repair_rounds < 0:
        raise ValueError("max_repair_rounds must be >= 0")
    return intent