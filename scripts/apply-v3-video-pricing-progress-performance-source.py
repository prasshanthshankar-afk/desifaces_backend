#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


def insert_in_function(text: str, function_marker: str, before: str, insertion: str, label: str) -> str:
    start = text.index(function_marker)
    pos = text.index(before, start)
    return text[:pos] + insertion + text[pos:]


# -----------------------------------------------------------------------------
# Premium Talking Video: actual-second customer pricing only.
# -----------------------------------------------------------------------------
orch_path = Path("services/svc-fusion-extension/app/app/services/longform_orchestrator.py")
orch = orch_path.read_text()

import_marker = "from app.services.stitch_service import compose_timeline, download_to_local, probe_duration_seconds, upload_final_mp4\n"
if "PREMIUM_ACTUAL_SECONDS_VARIANT" not in orch:
    orch = replace_once(
        orch,
        import_marker,
        import_marker
        + "from app.services.premium_actual_seconds_pricing import (\n"
        + "    PREMIUM_ACTUAL_SECONDS_ACTION,\n"
        + "    PREMIUM_ACTUAL_SECONDS_SKU,\n"
        + "    PREMIUM_ACTUAL_SECONDS_VARIANT,\n"
        + "    PREMIUM_CREDITS_PER_SECOND,\n"
        + "    PREMIUM_MIN_BILLABLE_SECONDS,\n"
        + "    is_premium_actual_seconds_variant,\n"
        + "    premium_actual_seconds_meta,\n"
        + "    premium_billable_seconds,\n"
        + ")\n",
        "premium pricing import",
    )

preview_marker = "PREMIUM_ACTUAL_SECONDS_PREVIEW_V1"
if preview_marker not in orch:
    preview_insert = f'''    # {preview_marker}: customer price follows actual duration; execution\n    # segmentation remains provider/internal metadata only.\n    if profile == "talking_video" and quality == "premium":\n        requested_for_pricing = (\n            _safe_int(payload.get("pricing_duration_sec"), 0)\n            or requested_duration_sec\n            or _pricing_duration_seconds(payload, tags)\n        )\n        actual_duration_sec = max(1, _safe_int(requested_for_pricing, 1))\n        billable_seconds = premium_billable_seconds(actual_duration_sec)\n        provider_limit_sec = max(1, _economy_provider_limit_sec(payload, tags))\n        execution_segment_plan = _economy_segment_plan_seconds(\n            actual_duration_sec,\n            segment_limit_sec=provider_limit_sec,\n        )\n        meta = {{\n            "longform_profile": profile,\n            "service_action": PREMIUM_ACTUAL_SECONDS_ACTION,\n            "variant_code": PREMIUM_ACTUAL_SECONDS_VARIANT,\n            "leaf_sku_code": PREMIUM_ACTUAL_SECONDS_SKU,\n            "aspect_ratio": _safe_str(payload.get("aspect_ratio")) or "9:16",\n            "camera_angle": _safe_str(payload.get("camera_angle")) or _safe_str(tags.get("camera_angle")),\n            "camera_framing": _safe_str(payload.get("camera_framing")) or _safe_str(tags.get("camera_framing")),\n            "camera_motion_style": _safe_str(payload.get("camera_motion_style")) or _safe_str(tags.get("camera_motion_style")),\n            "background_mode": _safe_str(payload.get("background_mode")) or _safe_str(tags.get("background_mode")),\n            "quality_tier": quality,\n            "provider_hint": _provider_hint(payload, tags),\n            "execution_provider_family": _execution_provider_family(payload, tags),\n            "preview_fingerprint": request_fingerprint,\n            "estimated_duration_sec": actual_duration_sec,\n            "duration_sec": actual_duration_sec,\n            "requested_duration_sec": requested_duration_sec or actual_duration_sec,\n            "detected_audio_duration_sec": _safe_int(_effective_voice_audio_source(payload).get("duration_sec"), 0),\n            "provider_limit_sec": provider_limit_sec,\n            "units": str(billable_seconds),\n            "requested_units": str(billable_seconds),\n            "quantity": billable_seconds,\n            "billing_quantity": billable_seconds,\n            "segmented": len(execution_segment_plan) > 1,\n            "segment_count": len(execution_segment_plan),\n            "segment_durations_sec": execution_segment_plan,\n            "pricing_strategy": "premium_actual_seconds",\n            "selected_mode": "talking_video_premium",\n            **premium_actual_seconds_meta(actual_duration_sec),\n        }}\n        return PricingPreviewSpec(\n            user_id=str(user_id),\n            service_name="svc-fusion-extension",\n            service_action=PREMIUM_ACTUAL_SECONDS_ACTION,\n            sku_code=PREMIUM_ACTUAL_SECONDS_VARIANT,\n            units=str(billable_seconds),\n            external_ref_type="longform_job_preview",\n            external_ref_id=f"preview:{{request_fingerprint}}",\n            idempotency_key=f"svc-fusion-extension:preview:{{user_id}}:{{request_fingerprint}}",\n            meta=meta,\n        )\n\n'''
    orch = insert_in_function(
        orch,
        "def build_longform_pricing_preview_spec",
        '    if profile == "talking_video" and quality in {"economy", "premium"}:\n',
        preview_insert,
        "premium preview branch",
    )

reserve_marker = "PREMIUM_ACTUAL_SECONDS_RESERVE_V1"
if reserve_marker not in orch:
    reserve_insert = f'''    # {reserve_marker}: reserve the parent against the authoritative\n    # actual-duration quote. Never derive customer units from child segment count.\n    if profile == "talking_video" and quality == "premium":\n        requested_duration_sec = _requested_duration_hint_seconds(payload, tags)\n        requested_for_pricing = (\n            _safe_int(payload.get("pricing_duration_sec"), 0)\n            or requested_duration_sec\n            or _pricing_duration_seconds(payload, tags)\n        )\n        actual_duration_sec = max(1, _safe_int(requested_for_pricing, 1))\n        billable_seconds = premium_billable_seconds(actual_duration_sec)\n        units = str(billable_seconds)\n        provider_limit_sec = max(1, _economy_provider_limit_sec(payload, tags))\n        execution_segment_plan = _economy_segment_plan_seconds(\n            actual_duration_sec,\n            segment_limit_sec=provider_limit_sec,\n        )\n        confirmation = _as_dict_loose(payload.get("pricing_confirmation"))\n        confirmed_variant_code = _safe_str(confirmation.get("variant_code"))\n        confirmed_units = _safe_str(confirmation.get("estimated_units")) or _safe_str(confirmation.get("requested_units"))\n        if confirmed_variant_code and confirmed_variant_code != PREMIUM_ACTUAL_SECONDS_VARIANT:\n            raise PricingClientError("PRICING_CONFIRMATION_VARIANT_MISMATCH")\n        if confirmed_units and _safe_int(confirmed_units, -1) != billable_seconds:\n            raise PricingClientError("PRICING_CONFIRMATION_UNITS_MISMATCH")\n\n        if not bool(getattr(client, "enabled", False)):\n            if _pricing_required():\n                raise PricingClientError(f"PRICING_CLIENT_DISABLED: {{_pricing_disabled_reason()}}")\n            pricing = {{\n                "enabled": False,\n                "state": "disabled",\n                "variant_code": PREMIUM_ACTUAL_SECONDS_VARIANT,\n                "sku_code": PREMIUM_ACTUAL_SECONDS_SKU,\n                "leaf_sku_code": PREMIUM_ACTUAL_SECONDS_SKU,\n                "message": _pricing_disabled_reason(),\n            }}\n            await _persist_job_pricing(conn, job_id, pricing, build_pricing_summary(pricing))\n            return pricing\n\n        reserve_meta = {{\n            "longform_profile": profile,\n            "longform_job_id": str(job_id),\n            "service_job_id": str(job_id),\n            "service_job_table": "longform_jobs",\n            "pricing_entity_kind": "service_job",\n            "omit_studio_job_id": True,\n            "external_ref_type": "longform_job",\n            "service_name": "svc-fusion-extension",\n            "service_action": PREMIUM_ACTUAL_SECONDS_ACTION,\n            "variant_code": PREMIUM_ACTUAL_SECONDS_VARIANT,\n            "leaf_sku_code": PREMIUM_ACTUAL_SECONDS_SKU,\n            "aspect_ratio": _safe_str(payload.get("aspect_ratio")) or "9:16",\n            "camera_angle": _safe_str(payload.get("camera_angle")) or _safe_str(tags.get("camera_angle")),\n            "camera_framing": _safe_str(payload.get("camera_framing")) or _safe_str(tags.get("camera_framing")),\n            "camera_motion_style": _safe_str(payload.get("camera_motion_style")) or _safe_str(tags.get("camera_motion_style")),\n            "background_mode": _safe_str(payload.get("background_mode")) or _safe_str(tags.get("background_mode")),\n            "quality_tier": quality,\n            "provider_hint": _provider_hint(payload, tags),\n            "execution_provider_family": _execution_provider_family(payload, tags),\n            "estimated_duration_sec": actual_duration_sec,\n            "duration_sec": actual_duration_sec,\n            "requested_duration_sec": requested_duration_sec or actual_duration_sec,\n            "detected_audio_duration_sec": _safe_int(_effective_voice_audio_source(payload).get("duration_sec"), 0),\n            "provider_limit_sec": provider_limit_sec,\n            "units": units,\n            "requested_units": units,\n            "quantity": billable_seconds,\n            "billing_quantity": billable_seconds,\n            "segmented": len(execution_segment_plan) > 1,\n            "segment_count": len(execution_segment_plan),\n            "segment_durations_sec": execution_segment_plan,\n            "pricing_strategy": "premium_actual_seconds",\n            "selected_mode": "talking_video_premium",\n            **premium_actual_seconds_meta(actual_duration_sec),\n        }}\n        reserve_spec = PricingReserveSpec(\n            user_id=str(user_id),\n            service_name="svc-fusion-extension",\n            service_action=PREMIUM_ACTUAL_SECONDS_ACTION,\n            sku_code=PREMIUM_ACTUAL_SECONDS_VARIANT,\n            units=units,\n            external_ref_type="longform_job",\n            external_ref_id=str(job_id),\n            idempotency_key=f"svc-fusion-extension:job:{{job_id}}:reserve",\n            meta=reserve_meta,\n            quote_id=_safe_str(confirmation.get("quote_id")),\n            preview_fingerprint=_safe_str(confirmation.get("preview_fingerprint")),\n        )\n        resp = await client.reserve(build_reserve_request(reserve_spec))\n        artifact = make_reserved_artifact(\n            resp,\n            service_name="svc-fusion-extension",\n            service_action=PREMIUM_ACTUAL_SECONDS_ACTION,\n            sku_code=PREMIUM_ACTUAL_SECONDS_VARIANT,\n            estimated_units=units,\n            unit_type="second",\n            meta=reserve_meta,\n        )\n        pricing = dict(artifact.get("pricing") or {{}})\n        pricing["enabled"] = True\n        pricing["state"] = "reserved"\n        pricing["quote_id"] = _safe_str(getattr(resp, "quote_id", None)) or pricing.get("quote_id") or _safe_str(confirmation.get("quote_id"))\n        pricing["preview_fingerprint"] = _safe_str(getattr(resp, "preview_fingerprint", None)) or pricing.get("preview_fingerprint") or _safe_str(confirmation.get("preview_fingerprint"))\n        pricing["variant_code"] = PREMIUM_ACTUAL_SECONDS_VARIANT\n        pricing["sku_code"] = PREMIUM_ACTUAL_SECONDS_SKU\n        pricing["leaf_sku_code"] = PREMIUM_ACTUAL_SECONDS_SKU\n        summary = dict(artifact.get("pricing_summary") or build_pricing_summary(pricing))\n        await _backfill_reservation_leaf_sku(\n            conn,\n            reservation_id=_safe_str(pricing.get("reservation_id")) or _safe_str(getattr(resp, "reservation_id", None)),\n            variant_code=PREMIUM_ACTUAL_SECONDS_VARIANT,\n            leaf_sku_code=PREMIUM_ACTUAL_SECONDS_SKU,\n        )\n        await _persist_job_pricing(conn, job_id, pricing, summary)\n        return pricing\n\n'''
    orch = insert_in_function(
        orch,
        "async def reserve_longform_pricing_for_job",
        '    if profile == "talking_video" and quality in {"economy", "premium"}:\n',
        reserve_insert,
        "premium reserve branch",
    )

commit_marker = "PREMIUM_ACTUAL_SECONDS_COMMIT_V1"
if commit_marker not in orch:
    old = '''    is_talking_video_bucket = (\n        reserved_bucket_code.startswith("economy_")\n        or reserved_bucket_code.startswith("premium_")\n        or reserved_variant_code.startswith("TALKING_VIDEO_ECONOMY_")\n        or reserved_variant_code.startswith("TALKING_VIDEO_PREMIUM_")\n    )\n\n    if is_talking_video_bucket:\n'''
    new = f'''    # {commit_marker}: keep final charge at or below the confirmed\n    # actual-second reservation. A provider/stitch timing drift cannot increase\n    # the customer's confirmed price.\n    is_premium_actual_seconds = (\n        is_premium_actual_seconds_variant(reserved_variant_code)\n        or (\n            str(pricing_meta.get("billing_basis") or "").strip().lower() == "actual_seconds"\n            and str(_pricing_profile({{}}, tags)).strip().lower() == "talking_video"\n        )\n    )\n    is_talking_video_bucket = (\n        reserved_bucket_code.startswith("economy_")\n        or reserved_bucket_code.startswith("premium_")\n        or reserved_variant_code.startswith("TALKING_VIDEO_ECONOMY_")\n        or (reserved_variant_code.startswith("TALKING_VIDEO_PREMIUM_") and not is_premium_actual_seconds)\n    )\n\n    if is_premium_actual_seconds:\n        effective_variant_code = PREMIUM_ACTUAL_SECONDS_VARIANT\n        effective_leaf_sku_code = PREMIUM_ACTUAL_SECONDS_SKU\n        effective_bucket_code = ""\n        effective_bucket_max_sec = None\n        reserved_billable_seconds = max(\n            PREMIUM_MIN_BILLABLE_SECONDS,\n            _safe_int(pricing.get("estimated_units"), 0)\n            or _safe_int(pricing_meta.get("billable_seconds"), 0)\n            or PREMIUM_MIN_BILLABLE_SECONDS,\n        )\n        measured_duration = (\n            final_duration_sec\n            or pricing_meta.get("actual_duration_sec")\n            or pricing_meta.get("duration_sec")\n            or reserved_billable_seconds\n        )\n        measured_billable_seconds = premium_billable_seconds(measured_duration)\n        units = str(min(reserved_billable_seconds, measured_billable_seconds))\n        minutes = _minutes_int_from_duration(measured_duration)\n        service_action = reserved_service_action or PREMIUM_ACTUAL_SECONDS_ACTION\n    elif is_talking_video_bucket:\n'''
    orch = replace_once(orch, old, new, "premium commit branch")

    meta_anchor = '''        "minutes": minutes,\n        "talking_video_bucket_code": effective_bucket_code or None,\n'''
    meta_new = '''        "minutes": minutes,\n        "billing_basis": "actual_seconds" if is_premium_actual_seconds else pricing_meta.get("billing_basis"),\n        "billable_seconds": _safe_int(units, 0) if is_premium_actual_seconds else pricing_meta.get("billable_seconds"),\n        "min_billable_seconds": PREMIUM_MIN_BILLABLE_SECONDS if is_premium_actual_seconds else pricing_meta.get("min_billable_seconds"),\n        "credits_per_second": PREMIUM_CREDITS_PER_SECOND if is_premium_actual_seconds else pricing_meta.get("credits_per_second"),\n        "platform_neutral": True if is_premium_actual_seconds else pricing_meta.get("platform_neutral"),\n        "provider_neutral": True if is_premium_actual_seconds else pricing_meta.get("provider_neutral"),\n        "talking_video_bucket_code": effective_bucket_code or None,\n'''
    orch = replace_once(orch, meta_anchor, meta_new, "premium commit metadata")

orch_path.write_text(orch)


# -----------------------------------------------------------------------------
# Video progress: expose real parent + segment state, not a UI-invented timer.
# -----------------------------------------------------------------------------
model_path = Path("services/svc-fusion-extension/app/app/domain/models.py")
model = model_path.read_text()
if "progress: Dict[str, Any]" not in model:
    anchor = "    runReceipt: Optional[Dict[str, Any]] = None\n"
    if anchor not in model:
        # Older model shape: add immediately after completed_segments.
        anchor = "    completed_segments: int\n"
        model = replace_once(model, anchor, anchor + "    progress: Dict[str, Any] = Field(default_factory=dict)\n", "longform progress model")
    else:
        model = replace_once(model, anchor, anchor + "    progress: Dict[str, Any] = Field(default_factory=dict)\n", "longform progress model")
model_path.write_text(model)

route_path = Path("services/svc-fusion-extension/app/app/api/routes/longform.py")
route = route_path.read_text()
if "LONGFORM_PROGRESS_V1" not in route:
    route = replace_once(
        route,
        "import os\nfrom typing import Any, Dict, Optional\n",
        "import os\nfrom datetime import datetime, timezone\nfrom typing import Any, Dict, Optional\n",
        "longform datetime import",
    )
    helper_anchor = "\ndef _clamp_fusion_duration(sec: int) -> int:\n"
    helper = '''\n# LONGFORM_PROGRESS_V1: backend-derived progress shared by every client.\ndef _longform_progress(job: Any, segments: list[Any]) -> Dict[str, Any]:\n    status_value = str(job.get("status") or "queued").strip().lower()\n    total = int(job.get("total_segments") or len(segments) or 0)\n    states = [str(s.get("status") or "queued").strip().lower() for s in segments]\n    completed = sum(1 for s in states if s == "succeeded")\n    failed = sum(1 for s in states if s in {"failed", "error", "canceled", "cancelled"})\n    running_states = {\n        "audio_running", "provider_running", "provider_processing", "video_running",\n        "running", "processing", "submitted", "fallback_running",\n        "provider_degraded_retrying", "switching_to_fallback",\n    }\n    running = sum(1 for s in states if s in running_states)\n    queued = sum(1 for s in states if s == "queued")\n\n    weights = {\n        "queued": 0.0,\n        "audio_running": 0.20,\n        "submitted": 0.40,\n        "provider_running": 0.55,\n        "provider_processing": 0.55,\n        "video_running": 0.65,\n        "running": 0.55,\n        "processing": 0.55,\n        "provider_degraded_retrying": 0.50,\n        "switching_to_fallback": 0.50,\n        "fallback_running": 0.60,\n        "succeeded": 1.0,\n        "failed": 1.0,\n        "error": 1.0,\n    }\n    segment_fraction = (sum(weights.get(s, 0.25 if s not in {"queued", ""} else 0.0) for s in states) / max(1, total)) if total else 0.0\n\n    if status_value == "pricing_pending":\n        percent, stage, message = 5, "pricing", "Confirming the video price and reserving credits…"\n    elif status_value == "queued":\n        percent, stage, message = 10, "queued", "Video is queued and preparing parallel renders…"\n    elif status_value in {"stitching", "stitching_running", "stitching_active"}:\n        percent, stage, message = 92, "stitching", "All video parts are ready. Building your final video…"\n    elif status_value == "succeeded":\n        percent, stage, message = 100, "complete", "Your video is ready."\n    elif status_value in {"failed", "error", "blocked", "canceled", "cancelled"}:\n        percent = max(0, min(99, int(round(10 + (segment_fraction * 78)))))\n        stage, message = "stopped", str(job.get("error_message") or "Video generation stopped before completion.")\n    else:\n        percent = max(10, min(89, int(round(10 + (segment_fraction * 78)))))\n        stage = "rendering"\n        if total:\n            message = f"Rendering video parts in parallel — {completed} of {total} complete."\n        else:\n            message = "Rendering your video…"\n\n    elapsed = 0\n    created_at = job.get("created_at")\n    if created_at:\n        try:\n            if isinstance(created_at, str):\n                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))\n            if created_at.tzinfo is None:\n                created_at = created_at.replace(tzinfo=timezone.utc)\n            elapsed = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds()))\n        except Exception:\n            elapsed = 0\n    delayed = status_value not in {"succeeded", "failed", "error", "blocked", "canceled", "cancelled"} and elapsed >= 180\n    delay_message = (\n        "This video is taking longer than usual, but the job is still active. "\n        "You can leave this screen and return later; your progress is preserved."\n        if delayed else None\n    )\n    return {\n        "percent": percent,\n        "stage": stage,\n        "message": message,\n        "segments_total": total,\n        "segments_completed": completed,\n        "segments_running": running,\n        "segments_queued": queued,\n        "segments_failed": failed,\n        "elapsed_seconds": elapsed,\n        "is_delayed": delayed,\n        "delay_message": delay_message,\n        "source": "backend_segment_state",\n    }\n\n'''
    route = route.replace(helper_anchor, helper + helper_anchor, 1)

    # Load segment state in the canonical job status endpoint.
    status_anchor = '''        pricing_view, pricing_summary_view = await _load_latest_pricing_view(conn, str(row["id"]), {"tags": tags})\n        run_receipt_view = _build_run_receipt_view(pricing_view, pricing_summary_view)\n\n        return LongformJobView(\n'''
    status_new = '''        pricing_view, pricing_summary_view = await _load_latest_pricing_view(conn, str(row["id"]), {"tags": tags})\n        run_receipt_view = _build_run_receipt_view(pricing_view, pricing_summary_view)\n        progress_rows = await segs_repo.list_segments_for_job(conn, str(row["id"]))\n        progress_view = _longform_progress(row, [dict(item) for item in progress_rows])\n\n        return LongformJobView(\n'''
    route = replace_once(route, status_anchor, status_new, "longform status progress query")
    response_anchor = '''            completed_segments=row["completed_segments"],\n            final_video_url=final_url,\n'''
    response_new = '''            completed_segments=row["completed_segments"],\n            progress=progress_view,\n            final_video_url=final_url,\n'''
    route = replace_once(route, response_anchor, response_new, "longform status progress response")
route_path.write_text(route)


# -----------------------------------------------------------------------------
# Face progress: enrich existing backend progress only; execution stays unchanged.
# -----------------------------------------------------------------------------
face_path = Path("services/svc-face/app/app/services/creator_orchestrator.py")
face = face_path.read_text()
if "FACE_PROGRESS_DETAIL_V1" not in face:
    face = replace_once(
        face,
        "from dataclasses import dataclass\n",
        "from dataclasses import dataclass\nfrom datetime import datetime, timezone\n",
        "face datetime import",
    )
    call_anchor = "        progress = self._get_progress_info(status_enum, len(variants), requested)\n"
    call_new = '''        progress = self._get_progress_info(\n            status_enum,\n            len(variants),\n            requested,\n            created_at=self._row_get(job, "created_at", None),\n        )\n'''
    face = replace_once(face, call_anchor, call_new, "face progress call")

    old_method = '''    def _get_progress_info(\n        self,\n        status: JobStatus,\n        variants_count: int,\n        requested: Optional[int],\n    ) -> Optional[Dict[str, Any]]:\n        if status == JobStatus.RUNNING:\n            base: Dict[str, Any] = {\n                "message": "Generating creator platform variants...",\n                "current_step": "Image generation",\n                "variants_completed": variants_count,\n            }\n            if requested is not None:\n                base["variants_requested"] = requested\n            return base\n\n        if status == JobStatus.SUCCEEDED:\n            base = {\n                "message": f"Generated {variants_count} variants successfully",\n                "variants_completed": variants_count,\n            }\n            if requested is not None:\n                base["variants_requested"] = requested\n            return base\n\n        return None\n'''
    new_method = '''    # FACE_PROGRESS_DETAIL_V1: status is derived from persisted job/variant state.\n    def _get_progress_info(\n        self,\n        status: JobStatus,\n        variants_count: int,\n        requested: Optional[int],\n        *,\n        created_at: Any = None,\n    ) -> Optional[Dict[str, Any]]:\n        requested_count = max(1, int(requested or variants_count or 1))\n        completed_count = max(0, min(int(variants_count or 0), requested_count))\n        elapsed = 0\n        if created_at:\n            try:\n                dt = created_at\n                if isinstance(dt, str):\n                    dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))\n                if dt.tzinfo is None:\n                    dt = dt.replace(tzinfo=timezone.utc)\n                elapsed = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))\n            except Exception:\n                elapsed = 0\n\n        delayed = status in {JobStatus.QUEUED, JobStatus.RUNNING} and elapsed >= 90\n        delay_message = (\n            "Face generation is taking longer than usual, but your job is still active. "\n            "You can leave this screen and return later; your progress is preserved."\n            if delayed else None\n        )\n\n        if status == JobStatus.QUEUED:\n            percent = 5\n            stage = "queued"\n            message = "Face generation is queued and preparing your request…"\n        elif status == JobStatus.RUNNING:\n            # Variant generation is genuinely parallel. Completed variants are\n            # authoritative; keep unfinished work below 95% until persisted.\n            percent = max(10, min(94, int(round(10 + 84 * (completed_count / requested_count)))))\n            stage = "generating"\n            message = f"Generating Face variants — {completed_count} of {requested_count} complete."\n        elif status == JobStatus.SUCCEEDED:\n            percent = 100\n            stage = "complete"\n            message = f"Generated {completed_count} Face variants successfully."\n        elif status in {JobStatus.FAILED, JobStatus.CANCELLED}:\n            percent = max(0, min(99, int(round(10 + 84 * (completed_count / requested_count)))))\n            stage = "stopped"\n            message = "Face generation stopped before all requested variants completed."\n        else:\n            return None\n\n        return {\n            "percent": percent,\n            "stage": stage,\n            "message": message,\n            "current_step": "Image generation" if stage == "generating" else stage,\n            "variants_completed": completed_count,\n            "variants_requested": requested_count,\n            "elapsed_seconds": elapsed,\n            "is_delayed": delayed,\n            "delay_message": delay_message,\n            "source": "backend_job_state",\n        }\n'''
    face = replace_once(face, old_method, new_method, "face progress method")
face_path.write_text(face)

print("V3_VIDEO_PRICING_PROGRESS_SOURCE_PATCH=PASS")
