from __future__ import annotations

from app.domain.enums import AspectRatio
from app.domain.models import VideoSettings, Dimension


def resolve_dimension(video: VideoSettings) -> Dimension:
    """
    Always returns a dimension. AV4 behaves best with explicit width/height.
    """
    # Prefer explicit dimension if provided
    if video.dimension:
        return video.dimension

    # Otherwise map by aspect ratio to sane defaults (720p-ish)
    if video.aspect_ratio == AspectRatio.ar_9_16:
        return Dimension(width=720, height=1280)
    if video.aspect_ratio == AspectRatio.ar_16_9:
        return Dimension(width=1280, height=720)
    if video.aspect_ratio == AspectRatio.ar_1_1:
        return Dimension(width=1024, height=1024)

    # Safe default: portrait
    return Dimension(width=720, height=1280)