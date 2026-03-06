# services/svc-music/app/app/services/music_orchestrator.py
from __future__ import annotations

import asyncio
import copy
import inspect
import os
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from app.db import get_pool
from app.domain.enums import MusicJobStatus, MusicTrackType
from app.repos.music_jobs_repo import MusicJobsRepo
from app.repos.music_tracks_repo import MusicTracksRepo
from app.repos.steps_repo import StepsRepo
from app.services.clip_manifest_service import ClipManifestService
from app.services.music_tools import ConcreteMusicTools
from app.services.music_orchestrator_common import (
    _as_dict,
    _as_list,
    _clamp_int,
    _normalize_jsonb_payload,
    _normalize_mode,
    _normalize_outputs,
)
from app.services.music_orchestrator_studio_jobs import (
    ensure_studio_job_envelope,
    persist_studio_payload_best_effort,
    update_studio_job_status_best_effort,
)
from app.services.music_orchestrator_voice_ref import resolve_voice_ref_sas_url
from app.services.music_orchestrator_faces import ensure_music_job_performer_faces
from app.services.music_orchestrator_audio import (
    pick_audio_url_for_probe,
    maybe_probe_audio_and_update_computed,
)
from app.services.music_orchestrator_broll import ensure_broll_for_manifest
from app.services.music_orchestrator_montage import render_montage_and_upload
from app.services.preset_selection_service import PresetSelectionService

# IMPORTANT: routes import these from app.services.music_orchestrator
# so we re-export them here for backward compatibility.
from app.services.music_orchestrator_status_publish import (
    get_video_job_status,
    publish_project_to_video_or_fusion,
)

from .music_graph import MusicGraphState, run_video_pipeline


async def enqueue_video_job(job_id: UUID) -> None:
    # DB polling worker: can be no-op. If you later add a real queue (Redis/ASB),
    # enqueue a message here.
    return None


def _truthy(v: Any) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _error_str(e: BaseException) -> str:
    try:
        msg = str(e)
    except Exception:
        msg = "unknown_error"
    return f"{type(e).__name__}:{msg}"


def _pick_first_str(*vals: Any) -> Optional[str]:
    for v in vals:
        if isinstance(v, str):
            s = v.strip()
            if s:
                return s
    return None


# -----------------------------
# DesiFaces quality gates
# -----------------------------
def _allow_fallback_audio_env() -> bool:
    """
    DesiFaces quality policy:
      - Fallback audio is DEV-only and OFF by default.
      - Only allow if explicitly enabled.
    """
    if (os.getenv("DF_ALLOW_FALLBACK_AUDIO") or "").strip():
        return _truthy(os.getenv("DF_ALLOW_FALLBACK_AUDIO"))
    # Back-compat only if explicitly set
    if (os.getenv("MUSIC_ALLOW_NATIVE_FALLBACK") or "").strip():
        return _truthy(os.getenv("MUSIC_ALLOW_NATIVE_FALLBACK"))
    return False


def _is_fallback_audio_url(url: str) -> bool:
    u = (url or "").lower()
    return ("fallback_full_mix" in u) or ("fallback_native" in u)


def _extract_full_mix_url_from_state(state: MusicGraphState) -> str:
    for t in getattr(state, "tracks", []) or []:
        if str(getattr(t, "track_type", "")) == MusicTrackType.full_mix.value:
            meta = getattr(t, "meta", None)
            md = meta if isinstance(meta, dict) else {}
            url = _pick_first_str(md.get("url"), md.get("audio_master_url"), md.get("sas_url"), md.get("storage_ref"))
            return url or ""
    return ""


def _reject_fallback_audio_or_raise(*, input_json: Dict[str, Any], state: MusicGraphState, where: str) -> None:
    """
    If fallback audio is being used, fail fast unless DF_ALLOW_FALLBACK_AUDIO=1.
    This prevents "successful" humming outputs.
    """
    if _allow_fallback_audio_env():
        return

    computed = _as_dict(input_json.get("computed"))

    # Prefer explicit computed flags if present
    if _truthy(computed.get("audio_is_fallback")):
        reason = str(computed.get("audio_fallback_reason") or "fallback_audio_not_allowed")
        raise RuntimeError(f"audio_fallback_rejected:{where}:{reason}")

    # Otherwise detect from URLs
    full_mix_url = _extract_full_mix_url_from_state(state) or str(computed.get("audio_master_url") or "")
    if full_mix_url and _is_fallback_audio_url(full_mix_url):
        raise RuntimeError(f"audio_fallback_rejected:{where}:full_mix_is_fallback")


def _ensure_run_id(input_json: Dict[str, Any]) -> bool:
    """
    Ensure computed.run_id exists (stable per run). Returns True if mutated.
    """
    computed = _as_dict(input_json.get("computed"))
    rid = computed.get("run_id")
    if isinstance(rid, str) and rid.strip():
        return False
    computed["run_id"] = str(uuid4())
    input_json["computed"] = computed
    return True


def _canonicalize_performer_face_refs(input_json: Dict[str, Any]) -> bool:
    """
    Ensure computed.performer_face_image_url is populated from legacy keys if possible.
    Returns True if mutated.
    """
    computed = _as_dict(input_json.get("computed"))
    hints = _as_dict(input_json.get("provider_hints"))

    before = str(computed.get("performer_face_image_url") or "").strip()

    env_url = str(os.getenv("DF_PERFORMER_FACE_IMAGE_URL") or "").strip()
    url = _pick_first_str(
        env_url,
        hints.get("performer_face_image_url"),
        computed.get("performer_face_image_url"),
        computed.get("performer_a_image_url"),
        computed.get("performer_image_url"),
        computed.get("performer_face_url"),
        computed.get("face_image_url"),
        computed.get("face_url"),
    )

    if url and url != before:
        computed["performer_face_image_url"] = url
        input_json["computed"] = computed
        return True
    return False


def _sync_performer_video_aliases(input_json: Dict[str, Any]) -> bool:
    """
    Make performer video keys usable by montage/other components even if module uses different keying.
    Returns True if mutated.
    """
    computed = _as_dict(input_json.get("computed"))
    changed = False

    pv_url = str(computed.get("performer_video_url") or "").strip()
    if pv_url and not str(computed.get("performer_a_video_url") or "").strip():
        computed["performer_a_video_url"] = pv_url
        changed = True

    pv_asset = computed.get("performer_video_asset_id")
    if pv_asset and not computed.get("performer_a_video_asset_id"):
        computed["performer_a_video_asset_id"] = pv_asset
        changed = True

    if changed:
        input_json["computed"] = computed
    return changed


async def _persist_input_json_best_effort(
    *, jobs: MusicJobsRepo, job_id: UUID, input_json: Dict[str, Any]
) -> None:
    try:
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
    except Exception:
        pass
    try:
        await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)
    except Exception:
        pass


def _apply_manifest_fields_to_computed(*, input_json: Dict[str, Any], manifest: Dict[str, Any]) -> bool:
    """
    Mirror manifest fields into computed for quick access.
    Returns True if anything changed.
    """
    changed = False
    computed = _as_dict(input_json.get("computed"))

    preset_name = manifest.get("preset_name")
    if preset_name and computed.get("preset_name") != preset_name:
        computed["preset_name"] = preset_name
        changed = True

    scene = _as_dict(manifest.get("scene"))
    primary = scene.get("primary_tag")
    secondary = scene.get("secondary_tags")
    no_face = scene.get("no_face")
    tags_used = manifest.get("preset_tags_used") or scene.get("tags_used")

    if primary and computed.get("scene_primary_tag") != primary:
        computed["scene_primary_tag"] = primary
        changed = True

    if isinstance(secondary, list) and computed.get("scene_secondary_tags") != secondary:
        computed["scene_secondary_tags"] = secondary
        changed = True

    if no_face is not None and computed.get("scene_no_face") != bool(no_face):
        computed["scene_no_face"] = bool(no_face)
        changed = True

    if isinstance(tags_used, list) and computed.get("preset_tags_used") != tags_used:
        computed["preset_tags_used"] = tags_used
        changed = True

    exports = manifest.get("exports")
    if isinstance(exports, list) and computed.get("exports") != exports:
        computed["exports"] = exports
        changed = True

    if changed:
        input_json["computed"] = computed
    return changed


# -----------------------------
# Preset selection (DB-driven) - ensures presets are USED
# -----------------------------
def _explicit_preset_name(input_json: Dict[str, Any]) -> str:
    style = _as_dict(input_json.get("style"))
    explicit = (style.get("preset_name") or input_json.get("preset_name") or "").strip()
    return str(explicit or "").strip()


async def _fetch_available_preset_rows_from_db(*, pool) -> List[Dict[str, Any]]:
    """
    Pull full preset rows so PresetSelectionService can score using DB tags.
    """
    try:
        rows = await pool.fetch(
            """
            select
              name,
              tags,
              scene_primary_tag, scene_secondary_tags,
              mood_tag, energy_tag,
              face_mode, grade,
              shot_cookbook_json
            from public.music_style_presets
            order by name
            """
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


async def _ensure_preset_selection_for_job(
    *,
    pool,
    steps: StepsRepo,
    jobs: MusicJobsRepo,
    job_id: UUID,
    project_id: UUID,
    input_json: Dict[str, Any],
) -> Dict[str, Any]:
    computed = _as_dict(input_json.get("computed"))
    hints = _as_dict(input_json.get("provider_hints"))

    explicit = _explicit_preset_name(input_json)
    force = _truthy(hints.get("force_reselect_preset")) or _truthy(os.getenv("DF_FORCE_RESELECT_PRESET", "0"))

    rows = await _fetch_available_preset_rows_from_db(pool=pool)
    if not rows:
        rows = [{"name": "Urban Neon Hustle Peak"}]

    existing_name = str(computed.get("preset_name") or "").strip()

    if explicit:
        chosen_name = explicit
        source = "explicit"
        sel_primary = str(computed.get("scene_primary_tag") or "").strip() or "shot"
        sel_secondary = _as_list(computed.get("scene_secondary_tags"))
    else:
        if (not force) and existing_name and any(str(r.get("name") or "").strip() == existing_name for r in rows):
            chosen_name = existing_name
            source = "existing"
            sel_primary = str(computed.get("scene_primary_tag") or "").strip() or "shot"
            sel_secondary = _as_list(computed.get("scene_secondary_tags"))
        else:
            svc = PresetSelectionService()
            sel = svc.select_preset(
                project_id=str(project_id),
                job_id=str(job_id),
                input_json=input_json,
                available_presets=rows,
            )
            chosen_name = sel.preset_name
            source = "inferred_db"
            sel_primary = sel.primary_tag
            sel_secondary = sel.secondary_tags

    computed["preset_name"] = chosen_name

    tags_used: List[str] = []
    if sel_primary:
        tags_used.append(str(sel_primary).strip())
    for t in (sel_secondary or []):
        ts = str(t).strip()
        if ts and ts not in tags_used:
            tags_used.append(ts)

    for t in _as_list(computed.get("preset_tags_used")):
        ts = str(t).strip()
        if ts and ts not in tags_used:
            tags_used.append(ts)

    computed["scene_primary_tag"] = sel_primary or computed.get("scene_primary_tag") or "shot"
    computed["scene_secondary_tags"] = list(sel_secondary or computed.get("scene_secondary_tags") or [])
    computed["preset_tags_used"] = tags_used
    computed["scene_no_face"] = ("no_face" in tags_used) or ("broll" in tags_used)

    computed["preset_selection"] = {
        "preset_name": chosen_name,
        "primary_tag": computed.get("scene_primary_tag"),
        "secondary_tags": computed.get("scene_secondary_tags"),
        "source": source,
        "available_presets_count": len(rows),
        "forced": bool(force),
    }

    input_json["computed"] = computed
    await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="select_preset",
            status="succeeded",
            meta_json=computed["preset_selection"],
        )
    except Exception:
        pass

    return input_json


# -----------------------------
# Clip manifest
# -----------------------------
async def _ensure_clip_manifest_for_job(
    *,
    jobs: MusicJobsRepo,
    steps: StepsRepo,
    job_id: UUID,
    project_id: UUID,
    input_json: Dict[str, Any],
) -> Dict[str, Any]:
    computed = _as_dict(input_json.get("computed"))
    existing_raw = computed.get("clip_manifest")
    existing = existing_raw if isinstance(existing_raw, dict) else _as_dict(existing_raw)

    if isinstance(existing, dict) and _as_list(existing.get("clips")):
        if existing_raw is not existing:
            computed["clip_manifest"] = existing
            input_json["computed"] = computed
        if _apply_manifest_fields_to_computed(input_json=input_json, manifest=existing):
            await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)
        return input_json

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_clip_manifest",
            status="running",
            meta_json={"project_id": str(project_id)},
        )
    except Exception:
        pass

    manifest = await ClipManifestService().build_manifest(
        music_video_job_id=job_id,
        project_id=project_id,
        input_json=input_json,
    )

    computed["clip_manifest"] = manifest
    input_json["computed"] = computed
    _apply_manifest_fields_to_computed(input_json=input_json, manifest=manifest)

    await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

    try:
        await steps.upsert_step(
            job_id=job_id,
            step_code="ensure_clip_manifest",
            status="succeeded",
            meta_json={
                "has_clips": bool(_as_list(manifest.get("clips"))),
                "preset_name": manifest.get("preset_name"),
                "scene_primary_tag": _as_dict(manifest.get("scene")).get("primary_tag"),
            },
        )
    except Exception:
        pass

    return input_json


async def _progress_bump(*, pool, jobs: MusicJobsRepo, job_id: UUID, current: int, target: int) -> int:
    t = _clamp_int(int(target), 0, 100)
    c = _clamp_int(int(current), 0, 100)

    try:
        db_p = await pool.fetchval("select progress from public.music_video_jobs where id=$1", job_id)
        if db_p is not None:
            c = max(c, _clamp_int(int(db_p), 0, 100))
    except Exception:
        pass

    if t > c:
        try:
            await jobs.set_video_job_progress(job_id=job_id, progress=t)
        except Exception:
            return c
        return t
    return c


def _ensure_broll_supports_persist() -> bool:
    try:
        sig = inspect.signature(ensure_broll_for_manifest)  # type: ignore
        return "persist" in sig.parameters
    except Exception:
        return False


def _performer_faces_gate(input_json: Dict[str, Any]) -> Dict[str, Any]:
    hints = _as_dict(input_json.get("provider_hints"))
    computed = _as_dict(input_json.get("computed"))

    require_performer_videos = (
        _truthy(os.getenv("DF_REQUIRE_PERFORMER_VIDEOS", "0"))
        or _truthy(hints.get("require_performer_videos"))
        or _truthy(hints.get("require_performer_video"))
    )

    enable_performer_videos = (
        require_performer_videos
        or _truthy(os.getenv("DF_ENABLE_PERFORMER_VIDEOS", "0"))
        or _truthy(hints.get("enable_performer_videos"))
        or _truthy(hints.get("performer_videos_enabled"))
    )

    require_performer_faces = (
        require_performer_videos
        or _truthy(os.getenv("DF_REQUIRE_PERFORMER_FACES", "0"))
        or _truthy(hints.get("require_performer_faces"))
        or _truthy(hints.get("require_faces"))
    )

    face_image_url = _pick_first_str(
        os.getenv("DF_PERFORMER_FACE_IMAGE_URL"),
        hints.get("performer_face_image_url"),
        computed.get("performer_face_image_url"),
        computed.get("performer_a_image_url"),
        computed.get("performer_image_url"),
        computed.get("face_image_url"),
        computed.get("face_url"),
    )
    face_artifact_id = _pick_first_str(
        os.getenv("DF_PERFORMER_FACE_ARTIFACT_ID"),
        hints.get("performer_face_artifact_id"),
        computed.get("performer_face_artifact_id"),
    )

    return {
        "enable_performer_videos": bool(enable_performer_videos),
        "require_performer_videos": bool(require_performer_videos),
        "require_performer_faces": bool(require_performer_faces),
        "provided_face_image_url": face_image_url or None,
        "provided_face_artifact_id": face_artifact_id or None,
    }


def _get_performer_video_fn():
    try:
        from app.services.music_orchestrator_performer_video import (  # type: ignore
            ensure_performer_video_for_job,
        )

        return ensure_performer_video_for_job
    except Exception:
        return None


def _performer_video_present(input_json: Dict[str, Any]) -> bool:
    computed = _as_dict(input_json.get("computed"))
    keys = [
        "performer_video_url",
        "performer_video_asset_id",
        "performer_a_video_url",
        "performer_a_video_asset_id",
        "performer_videos",
        "performer_video_assets",
    ]
    for k in keys:
        v = computed.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, list) and len(v) > 0:
            return True
        if isinstance(v, dict) and len(v.keys()) > 0:
            return True
    return False


async def run_music_video_job(job_id: UUID) -> None:
    jobs = MusicJobsRepo()
    tracks_repo = MusicTracksRepo()
    steps = StepsRepo()
    pool = await get_pool()

    broll_task: Optional[asyncio.Task] = None

    try:
        job_row = await pool.fetchrow(
            """
            select id, project_id, status, progress, input_json, error, created_at, updated_at
            from public.music_video_jobs
            where id=$1
            limit 1
            """,
            job_id,
        )
        if not job_row:
            return

        job = dict(job_row)
        job_status = str(job.get("status") or "").strip()

        try:
            progress_int = int(job.get("progress") or 0)
        except Exception:
            progress_int = 0

        input_json = _normalize_jsonb_payload(job.get("input_json"))
        if not isinstance(input_json, dict):
            input_json = {}

        input_json["computed"] = _as_dict(input_json.get("computed"))
        if _ensure_run_id(input_json):
            await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

        proj_row = await pool.fetchrow("select * from public.music_projects where id=$1", job["project_id"])
        if not proj_row:
            if job_status in (MusicJobStatus.succeeded.value, MusicJobStatus.failed.value):
                return
            await jobs.set_video_job_failed(job_id=job_id, error="project_not_found")
            return

        proj = dict(proj_row)
        proj_user_id = UUID(str(proj["user_id"]))
        proj_id = UUID(str(proj["id"]))

        try:
            current_status = job_status or "queued"
            await ensure_studio_job_envelope(
                pool=pool,
                job_id=job_id,
                user_id=proj_user_id,
                project_id=proj_id,
                status=current_status,
                input_json=input_json,
                meta_json={
                    "source": "svc-music",
                    "music_project_id": str(proj_id),
                    "request_type": "music_video",
                },
            )
            await update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status=current_status,
                meta_patch={"svc": "svc-music", "music_project_id": str(proj_id)},
            )
            await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)
        except Exception:
            pass

        if job_status in (MusicJobStatus.succeeded.value, MusicJobStatus.failed.value):
            return

        await jobs.set_video_job_running(job_id=job_id)
        progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=5)

        try:
            await update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="running",
                meta_patch={"svc": "svc-music", "music_project_id": str(proj_id)},
            )
        except Exception:
            pass

        # Refresh voice ref SAS (optional) - do NOT wipe existing on transient failure
        computed = _as_dict(input_json.get("computed"))
        vr_raw = input_json.get("voice_ref_asset_id") or proj.get("voice_ref_asset_id")
        try:
            vr_id = UUID(str(vr_raw)) if vr_raw else None
        except Exception:
            vr_id = None

        try:
            voice_ref_url = await resolve_voice_ref_sas_url(
                project_id=proj_id,
                user_id=proj_user_id,
                voice_ref_asset_id=vr_id,
            )
        except Exception:
            voice_ref_url = None

        if voice_ref_url and computed.get("voice_ref_url") != voice_ref_url:
            computed["voice_ref_url"] = voice_ref_url
            input_json["computed"] = computed
            await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

        # -----------------------------------------
        # Ensure performer faces (GATED)
        # -----------------------------------------
        if _canonicalize_performer_face_refs(input_json):
            await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

        gate = _performer_faces_gate(input_json)
        enable_performer_videos = bool(gate["enable_performer_videos"])
        require_performer_videos = bool(gate["require_performer_videos"])
        require_performer_faces = bool(gate["require_performer_faces"])
        provided_face_image_url = gate["provided_face_image_url"]
        provided_face_artifact_id = gate["provided_face_artifact_id"]

        computed = _as_dict(input_json.get("computed"))
        computed["performer_video_enabled"] = bool(enable_performer_videos)
        computed["performer_video_required"] = bool(require_performer_videos)
        computed["require_performer_faces"] = bool(require_performer_faces)
        input_json["computed"] = computed
        await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

        if not enable_performer_videos and not require_performer_faces:
            computed = _as_dict(input_json.get("computed"))
            computed["performer_faces_skipped"] = True
            computed["performer_faces_skip_reason"] = "performer_videos_disabled"
            input_json["computed"] = computed
            await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)
            progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=10)
        else:
            if provided_face_image_url or provided_face_artifact_id:
                computed = _as_dict(input_json.get("computed"))
                computed["performer_faces_skipped"] = False
                computed["performer_faces_skip_reason"] = None
                if provided_face_image_url:
                    computed["performer_face_image_url"] = provided_face_image_url
                if provided_face_artifact_id:
                    computed["performer_face_artifact_id"] = provided_face_artifact_id
                input_json["computed"] = computed
                await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)
                progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=10)
            else:
                try:
                    input_json = await ensure_music_job_performer_faces(
                        jobs=jobs,
                        steps=steps,
                        job_id=job_id,
                        proj=proj,
                        input_json=input_json,
                    )

                    _canonicalize_performer_face_refs(input_json)
                    await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)
                    progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=10)

                    computed = _as_dict(input_json.get("computed"))
                    face_ok = bool(str(computed.get("performer_face_image_url") or "").strip()) or bool(
                        str(computed.get("performer_face_artifact_id") or "").strip()
                    )
                    if require_performer_faces and not face_ok:
                        raise RuntimeError("performer_videos_missing_face_ref")
                except Exception as e:
                    computed = _as_dict(input_json.get("computed"))
                    computed["performer_faces_skipped"] = True
                    computed["performer_faces_skip_reason"] = f"svc_face_error:{_error_str(e)}"
                    input_json["computed"] = computed
                    await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

                    if require_performer_faces:
                        await jobs.set_video_job_failed(job_id=job_id, error=f"ensure_performer_faces_failed:{_error_str(e)}")
                        try:
                            await update_studio_job_status_best_effort(
                                pool=pool,
                                job_id=job_id,
                                status="failed",
                                error_message=f"ensure_performer_faces_failed:{_error_str(e)}",
                                meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
                            )
                        except Exception:
                            pass
                        return

                    progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=10)

        # Probe early if audio already known (BYO/uploaded)
        try:
            pre_audio_url = pick_audio_url_for_probe(input_json)
            input_json = await maybe_probe_audio_and_update_computed(
                jobs=jobs,
                job_id=job_id,
                project_id=proj_id,
                input_json=input_json,
                audio_url=pre_audio_url,
            )
            progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=12)
        except Exception:
            pass

        # Preset selection before clip manifest + b-roll
        try:
            input_json = await _ensure_preset_selection_for_job(
                pool=pool,
                steps=steps,
                jobs=jobs,
                job_id=job_id,
                project_id=proj_id,
                input_json=input_json,
            )
            progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=14)
        except Exception:
            pass

        # Ensure clip manifest
        try:
            input_json = await _ensure_clip_manifest_for_job(
                jobs=jobs,
                steps=steps,
                job_id=job_id,
                project_id=proj_id,
                input_json=input_json,
            )
            progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=15)
        except Exception as e:
            await jobs.set_video_job_failed(job_id=job_id, error=f"ensure_clip_manifest_failed:{_error_str(e)}")
            try:
                await update_studio_job_status_best_effort(
                    pool=pool,
                    job_id=job_id,
                    status="failed",
                    error_message=f"ensure_clip_manifest_failed:{_error_str(e)}",
                    meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
                )
            except Exception:
                pass
            return

        # b-roll in parallel if supported
        try:
            broll_input = copy.deepcopy(input_json)
            if _ensure_broll_supports_persist():
                broll_task = asyncio.create_task(
                    ensure_broll_for_manifest(
                        steps=steps,
                        jobs=jobs,
                        job_id=job_id,
                        proj=proj,
                        input_json=broll_input,
                        persist=False,  # type: ignore[arg-type]
                    )
                )
            else:
                broll_task = None
        except Exception:
            broll_task = None

        requested_outputs = _normalize_outputs(input_json)

        state = MusicGraphState(
            job_id=job_id,
            project_id=proj_id,
            user_id=proj_user_id,
            mode=_normalize_mode(proj.get("mode")),
            duet_layout=str(proj.get("duet_layout") or "split_screen").lower(),
            language_hint=proj.get("language_hint") or "en-IN",
            scene_pack_id=proj.get("scene_pack_id"),
            camera_edit=str(proj.get("camera_edit") or "beat_cut").lower(),
            band_pack=proj.get("band_pack") or [],
            requested_outputs=requested_outputs,
        )

        tools = ConcreteMusicTools(job_id=job_id, project_id=state.project_id, user_id=state.user_id, input_json=input_json)

        state = await run_video_pipeline(state, tools, jobs=jobs, steps=steps)
        progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=82)

        # DesiFaces quality gate: do NOT proceed with fallback audio
        _reject_fallback_audio_or_raise(input_json=input_json, state=state, where="after_run_video_pipeline")

        computed = _as_dict(input_json.get("computed"))
        tool_computed = tools._computed()

        for k in (
            # lyrics/plan
            "lyrics_text",
            "lyrics_source_effective",
            "music_plan",
            "plan_summary",
            "voice_ref_url",
            # audio (CRITICAL)
            "audio_provider",
            "audio_provider_requested",
            "provider_request_id",
            "autopilot_provider_error",
            "audio_gen_error",
            "audio_master_url",
            "byo_audio_url",
            "audio_master_duration_ms",
            "audio_duration_ms",
            "audio_content_type",
            "audio_source",
            "audio_is_fallback",
            "audio_fallback_reason",
            "audio_genre_family",
            "audio_mood",
            "audio_bpm",
            "audio_instrumentation",
            "audio_style_prompt",
            # misc
            "exports",
            "performer_faces_skipped",
            "performer_faces_skip_reason",
            "performer_face_image_url",
            "performer_face_artifact_id",
            "run_id",
        ):
            if k in tool_computed and tool_computed.get(k) is not None:
                computed[k] = tool_computed.get(k)

        for k in ("preset_name", "scene_primary_tag", "scene_secondary_tags", "preset_tags_used"):
            if (k in tool_computed) and (tool_computed.get(k) is not None) and (computed.get(k) is None):
                computed[k] = tool_computed.get(k)

        for t in state.tracks:
            tt = str(getattr(t, "track_type", ""))
            meta = getattr(t, "meta", None)

            if tt == MusicTrackType.full_mix.value and isinstance(meta, dict):
                am = meta.get("audio_master_url") or meta.get("url") or meta.get("sas_url") or meta.get("storage_ref")
                if am:
                    computed["audio_master_url"] = str(am)
                    computed["byo_audio_url"] = str(am)

                dur = meta.get("audio_duration_ms") or meta.get("byo_duration_ms") or meta.get("duration_ms")
                if dur:
                    try:
                        computed["audio_master_duration_ms"] = int(dur)
                        computed["audio_duration_ms"] = int(dur)
                    except Exception:
                        pass

            if tt == MusicTrackType.timed_lyrics_json.value and isinstance(meta, dict):
                inline = meta.get("inline_json")
                if inline:
                    computed["timed_lyrics_json"] = inline

        input_json["computed"] = computed
        await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

        try:
            audio_url = pick_audio_url_for_probe(input_json)
            input_json = await maybe_probe_audio_and_update_computed(
                jobs=jobs,
                job_id=job_id,
                project_id=proj_id,
                input_json=input_json,
                audio_url=audio_url,
            )
        except Exception:
            pass

        computed = _as_dict(input_json.get("computed"))
        if not (isinstance(computed.get("clip_manifest"), dict) and _as_list(_as_dict(computed.get("clip_manifest")).get("clips"))):
            input_json = await _ensure_clip_manifest_for_job(
                jobs=jobs,
                steps=steps,
                job_id=job_id,
                project_id=proj_id,
                input_json=input_json,
            )

        for t in state.tracks:
            await tracks_repo.upsert_track(
                project_id=state.project_id,
                track_type=t.track_type,
                duration_ms=int(t.duration_ms or 0),
                artifact_id=t.artifact_id,
                media_asset_id=t.media_asset_id,
                meta_json=(t.meta if isinstance(t.meta, dict) else None),
            )

        # fold b-roll result
        if broll_task is not None:
            try:
                broll_res = await broll_task
                if isinstance(broll_res, dict):
                    maybe_input = broll_res
                    if isinstance(broll_res.get("input_json"), dict):
                        maybe_input = _as_dict(broll_res.get("input_json"))
                    broll_comp = _as_dict(_as_dict(maybe_input.get("computed")).get("broll"))
                else:
                    broll_comp = {}
                if broll_comp:
                    computed = _as_dict(input_json.get("computed"))
                    computed["broll"] = broll_comp
                    input_json["computed"] = computed
                    await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)
            except Exception:
                input_json = await ensure_broll_for_manifest(
                    steps=steps,
                    jobs=jobs,
                    job_id=job_id,
                    proj=proj,
                    input_json=input_json,
                )
        else:
            input_json = await ensure_broll_for_manifest(
                steps=steps,
                jobs=jobs,
                job_id=job_id,
                proj=proj,
                input_json=input_json,
            )

        progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=90)

        # -----------------------------------------
        # Performer video
        # -----------------------------------------
        gate2 = _performer_faces_gate(input_json)
        enable_perf_vid = bool(gate2["enable_performer_videos"])
        require_perf_vid = bool(gate2["require_performer_videos"])

        # Ensure face canonical ref again right before fusion call
        if enable_perf_vid and _canonicalize_performer_face_refs(input_json):
            await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

        if enable_perf_vid:
            # If we already have a performer video, skip regeneration
            if _performer_video_present(input_json):
                pass
            else:
                perf_fn = _get_performer_video_fn()
                if callable(perf_fn):
                    try:
                        input_json = await perf_fn(
                            steps=steps,
                            jobs=jobs,
                            job_id=job_id,
                            proj=proj,
                            input_json=input_json,
                        )
                        if _sync_performer_video_aliases(input_json):
                            pass
                        await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)
                    except Exception as e:
                        computed = _as_dict(input_json.get("computed"))
                        computed["performer_video_skipped"] = True
                        computed["performer_video_skip_reason"] = f"perf_video_error:{_error_str(e)}"
                        input_json["computed"] = computed
                        await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

                        if require_perf_vid:
                            await jobs.set_video_job_failed(job_id=job_id, error=f"performer_video_failed:{_error_str(e)}")
                            try:
                                await update_studio_job_status_best_effort(
                                    pool=pool,
                                    job_id=job_id,
                                    status="failed",
                                    error_message=f"performer_video_failed:{_error_str(e)}",
                                    meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
                                )
                            except Exception:
                                pass
                            return
                else:
                    # Fallback to tools implementation (keeps svc-music functional even if module missing)
                    try:
                        await tools.generate_performer_videos(state)  # type: ignore[attr-defined]
                        # tools mutates shared input_json in-place; persist
                        input_json["computed"] = _as_dict(input_json.get("computed"))
                        if _sync_performer_video_aliases(input_json):
                            pass
                        await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)
                    except Exception as e:
                        computed = _as_dict(input_json.get("computed"))
                        computed["performer_video_skipped"] = True
                        computed["performer_video_skip_reason"] = f"perf_video_error_tools:{_error_str(e)}"
                        input_json["computed"] = computed
                        await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

                        if require_perf_vid:
                            await jobs.set_video_job_failed(job_id=job_id, error=f"performer_video_failed:{_error_str(e)}")
                            try:
                                await update_studio_job_status_best_effort(
                                    pool=pool,
                                    job_id=job_id,
                                    status="failed",
                                    error_message=f"performer_video_failed:{_error_str(e)}",
                                    meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
                                )
                            except Exception:
                                pass
                            return
        else:
            computed = _as_dict(input_json.get("computed"))
            if computed.get("performer_video_skipped") is None:
                computed["performer_video_skipped"] = True
                computed["performer_video_skip_reason"] = "disabled"
                input_json["computed"] = computed
                await _persist_input_json_best_effort(jobs=jobs, job_id=job_id, input_json=input_json)

        if require_perf_vid and not _performer_video_present(input_json):
            await jobs.set_video_job_failed(job_id=job_id, error="performer_video_missing_after_generation")
            try:
                await update_studio_job_status_best_effort(
                    pool=pool,
                    job_id=job_id,
                    status="failed",
                    error_message="performer_video_missing_after_generation",
                    meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
                )
            except Exception:
                pass
            return

        # DesiFaces quality gate (defense-in-depth): block fallback audio before montage/publish
        _reject_fallback_audio_or_raise(input_json=input_json, state=state, where="before_montage")

        # Montage
        _, preview_asset_id, final_asset_id = await render_montage_and_upload(
            steps=steps,
            jobs=jobs,
            pool=pool,
            job_id=job_id,
            user_id=proj_user_id,
            project_id=proj_id,
            input_json=input_json,
        )
        progress_int = await _progress_bump(pool=pool, jobs=jobs, job_id=job_id, current=progress_int, target=97)

        await jobs.set_video_job_succeeded(
            job_id=job_id,
            preview_video_asset_id=preview_asset_id or state.preview_video_asset_id,
            final_video_asset_id=final_asset_id or state.final_video_asset_id,
            performer_a_video_asset_id=state.performer_a_video_asset_id,
            performer_b_video_asset_id=state.performer_b_video_asset_id,
        )

        try:
            await update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="succeeded",
                meta_patch={"music_project_id": str(proj_id), "svc": "svc-music"},
            )
        except Exception:
            pass

    except Exception as e:
        if broll_task is not None and not broll_task.done():
            try:
                broll_task.cancel()
            except Exception:
                pass

        try:
            await steps.upsert_step(job_id=job_id, step_code="job_failed", status="failed", meta_json={"error": _error_str(e)})
        except Exception:
            pass

        await jobs.set_video_job_failed(job_id=job_id, error=_error_str(e))

        try:
            try:
                proj_id_str = str(locals().get("proj_id"))  # type: ignore
            except Exception:
                proj_id_str = ""
            await update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="failed",
                error_message=_error_str(e),
                meta_patch={"music_project_id": proj_id_str, "svc": "svc-music"},
            )
        except Exception:
            pass

    finally:
        if broll_task is not None and not broll_task.done():
            try:
                broll_task.cancel()
            except Exception:
                pass


async def run_compose_job(job_id: UUID) -> None:
    await run_music_video_job(job_id)