# services/svc-marketing/app/app/services/orchestration/qc/output_gates.py
from __future__ import annotations

from typing import Any, Dict, List

from app.services.orchestration.errors import MarketingRunFailed


def compose_is_done(fmt: str, out: Dict[str, Any], carousel_slides: int) -> bool:
    if not isinstance(out, dict):
        return False
    if not out.get("caption_url") or not out.get("manifest_url"):
        return False

    if fmt in ("reel", "yt_short", "yt_long"):
        return bool(out.get("reel_cover_url"))
    if fmt == "story":
        return bool(out.get("story_url"))
    if fmt == "carousel":
        if not out.get("slide_01_url"):
            return False
        if carousel_slides >= 2 and not out.get("slide_02_url"):
            return False
        return True
    return False


def required_output_keys(fmt: str) -> List[str]:
    if fmt in ("reel", "yt_short", "yt_long"):
        return ["reel_url", "reel_cover_url", "caption_url", "manifest_url"]
    if fmt == "story":
        return ["story_url", "caption_url", "manifest_url"]
    if fmt == "carousel":
        return ["slide_01_url", "slide_02_url", "caption_url", "manifest_url"]
    return ["caption_url", "manifest_url"]


def assert_required_outputs_or_fail(fmt: str, out: Dict[str, Any]) -> None:
    missing = [k for k in required_output_keys(fmt) if not out.get(k)]
    if missing:
        raise MarketingRunFailed("MARKETING_MISSING_OUTPUTS", f"Missing outputs: {missing}", stage="compose")