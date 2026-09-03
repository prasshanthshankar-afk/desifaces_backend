#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import runpy

FACE_PATH = Path("services/svc-face/app/app/services/creator_orchestrator.py")
BASE_PATCHER = Path("scripts/apply-v3-video-pricing-progress-performance-source.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def replace_method(text: str, marker: str, replacement: str) -> str:
    start = text.find(marker)
    if start < 0:
        raise SystemExit(f"face progress function marker missing: {marker!r}")
    next_def = text.find("\n    def ", start + len(marker))
    next_async = text.find("\n    async def ", start + len(marker))
    candidates = [p for p in (next_def, next_async) if p >= 0]
    end = min(candidates) if candidates else len(text)
    suffix = text[end:]
    if replacement.endswith("\n") and suffix.startswith("\n"):
        return text[:start] + replacement.rstrip("\n") + suffix
    return text[:start] + replacement + suffix


face = FACE_PATH.read_text()
if "FACE_PROGRESS_DETAIL_V1" not in face:
    if "from datetime import datetime, timezone\n" not in face:
        face = replace_once(
            face,
            "from dataclasses import dataclass\n",
            "from dataclasses import dataclass\nfrom datetime import datetime, timezone\n",
            "face datetime import",
        )

    old_call = "        progress = self._get_progress_info(status_enum, len(variants), requested)\n"
    new_call = '''        progress = self._get_progress_info(\n            status_enum,\n            len(variants),\n            requested,\n            created_at=self._row_get(job, "created_at", None),\n        )\n'''
    if old_call in face:
        face = replace_once(face, old_call, new_call, "face progress call")
    elif "created_at=self._row_get(job, \"created_at\", None)" not in face:
        raise SystemExit("face progress call: neither legacy nor enriched call found")

    new_method = '''    # FACE_PROGRESS_DETAIL_V1: status is derived from persisted job/variant state.\n    def _get_progress_info(\n        self,\n        status: JobStatus,\n        variants_count: int,\n        requested: Optional[int],\n        *,\n        created_at: Any = None,\n    ) -> Optional[Dict[str, Any]]:\n        requested_count = max(1, int(requested or variants_count or 1))\n        completed_count = max(0, min(int(variants_count or 0), requested_count))\n        elapsed = 0\n        if created_at:\n            try:\n                dt = created_at\n                if isinstance(dt, str):\n                    dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))\n                if dt.tzinfo is None:\n                    dt = dt.replace(tzinfo=timezone.utc)\n                elapsed = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))\n            except Exception:\n                elapsed = 0\n\n        delayed = status in {JobStatus.QUEUED, JobStatus.RUNNING} and elapsed >= 90\n        delay_message = (\n            "Face generation is taking longer than usual, but your job is still active. "\n            "You can leave this screen and return later; your progress is preserved."\n            if delayed else None\n        )\n\n        if status == JobStatus.QUEUED:\n            percent = 5\n            stage = "queued"\n            message = "Face generation is queued and preparing your request…"\n        elif status == JobStatus.RUNNING:\n            percent = max(10, min(94, int(round(10 + 84 * (completed_count / requested_count)))))\n            stage = "generating"\n            message = f"Generating Face variants — {completed_count} of {requested_count} complete."\n        elif status == JobStatus.SUCCEEDED:\n            percent = 100\n            stage = "complete"\n            message = f"Generated {completed_count} Face variants successfully."\n        elif status in {JobStatus.FAILED, JobStatus.CANCELLED}:\n            percent = max(0, min(99, int(round(10 + 84 * (completed_count / requested_count)))))\n            stage = "stopped"\n            message = "Face generation stopped before all requested variants completed."\n        else:\n            return None\n\n        return {\n            "percent": percent,\n            "stage": stage,\n            "message": message,\n            "current_step": "Image generation" if stage == "generating" else stage,\n            "variants_completed": completed_count,\n            "variants_requested": requested_count,\n            "elapsed_seconds": elapsed,\n            "is_delayed": delayed,\n            "delay_message": delay_message,\n            "source": "backend_job_state",\n        }\n'''
    face = replace_method(face, "    def _get_progress_info(\n", new_method)
    FACE_PATH.write_text(face)

# The base patcher now sees FACE_PROGRESS_DETAIL_V1 and leaves Face alone while
# applying the Premium actual-second pricing and longform Video progress changes.
runpy.run_path(str(BASE_PATCHER), run_name="__main__")

print("V3_VIDEO_PRICING_PROGRESS_SOURCE_PATCH_V2=PASS")
