#!/usr/bin/env python3
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


# -----------------------------------------------------------------------------
# Face: do not silently declare the whole request successful when only some
# requested variants completed. Preserve successful outputs and charge only the
# successful count, but surface partial_success as a terminal product state.
# -----------------------------------------------------------------------------
face_path = Path("services/svc-face/app/app/services/creator_orchestrator.py")
face = face_path.read_text()

if "FACE_PARTIAL_SUCCESS_V1" not in face:
    face = replace_once(
        face,
        '            JobStatus.SUCCEEDED: "Face generation completed successfully",\n            JobStatus.FAILED: "Face generation failed",\n',
        '            JobStatus.SUCCEEDED: "Face generation completed successfully",\n            JobStatus.PARTIAL_SUCCESS: "Face generation completed with fewer variants than requested",\n            JobStatus.FAILED: "Face generation failed",\n',
        "Face partial status message",
    )

    face = replace_once(
        face,
        '''        elif status == JobStatus.SUCCEEDED:\n            percent = 100\n            stage = "complete"\n            message = f"Generated {completed_count} Face variants successfully."\n        elif status in {JobStatus.FAILED, JobStatus.CANCELLED}:\n''',
        '''        elif status == JobStatus.SUCCEEDED:\n            percent = 100\n            stage = "complete"\n            message = f"Generated {completed_count} Face variants successfully."\n        elif status == JobStatus.PARTIAL_SUCCESS:\n            percent = 100\n            stage = "partial"\n            missing_count = max(0, requested_count - completed_count)\n            message = (\n                f"{completed_count} of {requested_count} Face variants completed. "\n                f"{missing_count} variant{'s' if missing_count != 1 else ''} could not be completed. "\n                "Only completed variants are charged."\n            )\n        elif status in {JobStatus.FAILED, JobStatus.CANCELLED}:\n''',
        "Face partial progress",
    )

    face = replace_once(
        face,
        '        if status in {"succeeded", "failed", "cancelled"}:\n',
        '        if status in {"succeeded", "partial_success", "failed", "cancelled"}:\n',
        "Face recovery terminal statuses",
    )

    old_completion = '''            completed_count = await self._count_completed_variants(job_id)\n            if completed_count > 0:\n                pricing = await self._commit_pricing_for_job(\n                    job_id=job_id,\n                    user_id=user_id,\n                    pricing=pricing,\n                    actual_units=completed_count,\n                )\n                await self.jobs_repo.update_status(\n                    job_id,\n                    "succeeded",\n                    meta_patch={\n                        "variants_completed": completed_count,\n                        "variants_requested": variants_requested,\n                    },\n                )\n                await _emit_notification_best_effort(\n                    {\n                        "event_type": "FACE_READY",\n                        "category": "jobs",\n                        "priority": "important",\n                        "source_service": "svc-face",\n                        "source_ref_type": "job",\n                        "source_ref_id": str(job_id),\n                        "actor_user_id": None,\n                        "title": "Your Face output is ready",\n                        "body": "Your desifaces.ai Face generation completed successfully.",\n                        "action_route": "/notifications",\n                        "action_label": "View result",\n                        "image_url": None,\n                        "payload_json": {"job_id": str(job_id), "completed_variants": int(completed_count)},\n                        "metadata_json": {"job_id": str(job_id), "completed_variants": int(completed_count)},\n                        "dedupe_key": f"face-ready:{job_id}",\n                        "recipients": [{"user_id": str(user_id), "channels": {"in_app": True, "push": True, "email": True}}],\n                    },\n                    context={"job_id": str(job_id), "user_id": str(user_id), "event_type": "FACE_READY"},\n                )\n            else:\n'''

    new_completion = '''            # FACE_PARTIAL_SUCCESS_V1: preserve every successful output, but\n            # never hide a failed requested variant behind a generic succeeded state.\n            completed_count = await self._count_completed_variants(job_id)\n            final_variants_state = await self._load_variants_state(job_id)\n            failed_count = sum(\n                1\n                for vv in final_variants_state.values()\n                if str(self._coerce_dict(vv).get("status") or "").strip().lower() == "failed"\n            )\n            if completed_count > 0:\n                pricing = await self._commit_pricing_for_job(\n                    job_id=job_id,\n                    user_id=user_id,\n                    pricing=pricing,\n                    actual_units=completed_count,\n                )\n                final_status = (\n                    "succeeded"\n                    if completed_count >= variants_requested and failed_count == 0\n                    else "partial_success"\n                )\n                await self.jobs_repo.update_status(\n                    job_id,\n                    final_status,\n                    meta_patch={\n                        "variants_completed": completed_count,\n                        "variants_requested": variants_requested,\n                        "variants_failed": failed_count,\n                        "partial_success": final_status == "partial_success",\n                    },\n                )\n                event_type = "FACE_READY" if final_status == "succeeded" else "FACE_PARTIAL"\n                title = (\n                    "Your Face output is ready"\n                    if final_status == "succeeded"\n                    else "Some Face variants are ready"\n                )\n                body = (\n                    "Your desifaces.ai Face generation completed successfully."\n                    if final_status == "succeeded"\n                    else f"{completed_count} of {variants_requested} requested Face variants completed. Only completed variants are charged."\n                )\n                await _emit_notification_best_effort(\n                    {\n                        "event_type": event_type,\n                        "category": "jobs",\n                        "priority": "important",\n                        "source_service": "svc-face",\n                        "source_ref_type": "job",\n                        "source_ref_id": str(job_id),\n                        "actor_user_id": None,\n                        "title": title,\n                        "body": body,\n                        "action_route": "/notifications",\n                        "action_label": "View result",\n                        "image_url": None,\n                        "payload_json": {\n                            "job_id": str(job_id),\n                            "completed_variants": int(completed_count),\n                            "requested_variants": int(variants_requested),\n                            "failed_variants": int(failed_count),\n                            "partial_success": final_status == "partial_success",\n                        },\n                        "metadata_json": {\n                            "job_id": str(job_id),\n                            "completed_variants": int(completed_count),\n                            "requested_variants": int(variants_requested),\n                            "failed_variants": int(failed_count),\n                        },\n                        "dedupe_key": f"face-ready:{job_id}:{final_status}",\n                        "recipients": [{"user_id": str(user_id), "channels": {"in_app": True, "push": True, "email": True}}],\n                    },\n                    context={"job_id": str(job_id), "user_id": str(user_id), "event_type": event_type},\n                )\n            else:\n'''
    face = replace_once(face, old_completion, new_completion, "Face completion contract")

face_path.write_text(face)


# -----------------------------------------------------------------------------
# Video progress: providers do not expose a trustworthy fractional completion
# percentage. Running children therefore receive only a small activity credit;
# completed segments drive the bulk of the bar. This avoids showing ~60% while
# zero provider outputs have actually completed.
# -----------------------------------------------------------------------------
route_path = Path("services/svc-fusion-extension/app/app/api/routes/longform.py")
route = route_path.read_text()

if "TRUTHFUL_PROVIDER_PROGRESS_V1" not in route:
    old = '''    else:\n        percent = max(10, min(89, int(round(10 + (segment_fraction * 78)))))\n        stage = "rendering"\n        if total:\n            message = f"Rendering video parts in parallel — {completed} of {total} complete."\n        else:\n            message = "Rendering your video…"\n'''
    new = '''    else:\n        # TRUTHFUL_PROVIDER_PROGRESS_V1: provider APIs expose state, not a reliable\n        # fractional completion percentage. Completed outputs drive progress;\n        # active provider jobs add only a small bounded activity credit.\n        completed_fraction = (completed / max(1, total)) if total else 0.0\n        activity_credit = min(10.0, (10.0 * running / max(1, total))) if running else 0.0\n        percent = max(10, min(85, int(round(15 + (completed_fraction * 65) + activity_credit))))\n        stage = "rendering"\n        if total:\n            message = (\n                f"Rendering video parts in parallel — {completed} of {total} complete. "\n                f"{running} running now."\n            )\n        else:\n            message = "Rendering your video…"\n'''
    route = replace_once(route, old, new, "Truthful Video progress")

route_path.write_text(route)
print("FACE_VIDEO_ACTUAL_PERFORMANCE_PATCH=PASS")
