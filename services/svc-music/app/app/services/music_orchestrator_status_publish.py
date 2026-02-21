from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from app.domain.enums import MusicJobStatus, MusicTrackType
from app.domain.models import MusicJobStatusOut, PublishMusicIn, PublishMusicOut, TrackItem
from app.repos.music_jobs_repo import MusicJobsRepo
from app.repos.music_projects_repo import MusicProjectsRepo
from app.repos.music_tracks_repo import MusicTracksRepo
from app.repos.steps_repo import StepsRepo
from app.services.music_orchestrator_common import (
    _as_dict,
    _is_truthy,
    _normalize_jsonb_payload,
    _progress01,
    _progress_for_stage,
    _safe_stage,
    _infer_stage_from_progress,
    _track_url,
    _track_ct,
    _guess_audio_content_type,
)
from app.services.music_orchestrator_studio_jobs import persist_fusion_payload_best_effort
from app.services.music_orchestrator_voice_ref import resolve_url_from_refs, resolve_voice_ref_sas_url


def _pick_best_video_urls(*, computed: Dict[str, Any]) -> Dict[str, Optional[Any]]:
    """
    Canonical video URL selection order (for UI + status consumers):
      1) montage final (computed.final_video_url OR computed.video_outputs.final_url OR exports[0].url)
      2) montage preview (computed.preview_video_url OR computed.video_outputs.preview_url)
      3) performer video (computed.performer_video_url)
    """
    vo = _as_dict(computed.get("video_outputs"))

    # final: explicit field first
    final_url = str(computed.get("final_video_url") or "").strip() or None

    # fallback: video_outputs.{final_url,url}
    if not final_url:
        final_url = str(vo.get("final_url") or vo.get("url") or "").strip() or None

    # fallback: exports list
    if not final_url:
        exports = vo.get("exports")
        if isinstance(exports, list):
            for e in exports:
                if isinstance(e, dict):
                    u = str(e.get("url") or "").strip()
                    if u:
                        final_url = u
                        break

    # preview
    preview_url = str(computed.get("preview_video_url") or "").strip() or None
    if not preview_url:
        preview_url = str(vo.get("preview_url") or vo.get("preview") or "").strip() or None

    # performer
    performer_url = str(computed.get("performer_video_url") or "").strip() or None

    display_url = final_url or preview_url or performer_url
    if final_url or preview_url or vo.get("source"):
        display_source = "montage"
    elif performer_url:
        display_source = "performer"
    else:
        display_source = None

    return {
        "final_url": final_url,
        "preview_url": preview_url,
        "performer_url": performer_url,
        "display_url": display_url,
        "display_source": display_source,
        "exports": vo.get("exports") if isinstance(vo.get("exports"), list) else None,
        "video_outputs": vo if vo else None,
    }


async def get_video_job_status(*, job_id: UUID, user_id: UUID) -> Optional[MusicJobStatusOut]:
    jobs = MusicJobsRepo()
    projects = MusicProjectsRepo()
    tracks_repo = MusicTracksRepo()
    steps = StepsRepo()

    vrow = await jobs.get_music_video_job_row(job_id=job_id)
    payload: Dict[str, Any] = {}
    project_id: Optional[UUID] = None
    status_val: Optional[str] = None
    progress_raw: Any = None
    error_val: Any = None

    # Prefer canonical music_video_jobs input_json whenever available.
    if vrow:
        project_id = UUID(str(vrow["project_id"]))
        status_val = str(vrow.get("status") or "").strip()
        progress_raw = vrow.get("progress")
        error_val = vrow.get("error")
        payload = _normalize_jsonb_payload(vrow.get("input_json"))
    else:
        job = await jobs.get_video_job(job_id=job_id)
        if not job:
            return None
        project_id = UUID(str(job["project_id"]))
        status_val = str(job.get("status") or "").strip()
        progress_raw = job.get("progress")
        error_val = job.get("error")
        payload = _normalize_jsonb_payload(job.get("payload_json"))

    proj = await projects.get(project_id=project_id, user_id=user_id)
    if not proj:
        return None

    computed: Dict[str, Any] = _as_dict(payload.get("computed"))

    # --- Canonical video URL derivation for consumers ---
    picked = _pick_best_video_urls(computed=computed)

    # Normalize: if montage exists but final/preview fields aren't set, derive them.
    if picked.get("final_url") and not str(computed.get("final_video_url") or "").strip():
        computed["final_video_url"] = picked["final_url"]
    if picked.get("preview_url") and not str(computed.get("preview_video_url") or "").strip():
        computed["preview_video_url"] = picked["preview_url"]

    # Provide a stable UI-facing field (so frontends don't accidentally show performer_url)
    computed["display_video_url"] = picked.get("display_url")
    computed["display_video_source"] = picked.get("display_source")
    if picked.get("exports") is not None:
        computed["display_video_exports"] = picked.get("exports")

    # optional: include normalized video_outputs dict for debugging/visibility
    if picked.get("video_outputs") is not None and not isinstance(computed.get("video_outputs"), dict):
        computed["video_outputs"] = picked.get("video_outputs")

    clip_manifest_raw = computed.get("clip_manifest") or payload.get("clip_manifest")
    clip_manifest_dict = clip_manifest_raw if isinstance(clip_manifest_raw, dict) else _as_dict(clip_manifest_raw)
    clip_manifest: Optional[Dict[str, Any]] = clip_manifest_dict if clip_manifest_dict else None

    track_rows = await tracks_repo.list_by_project(project_id=project_id)
    last = await steps.latest_step(job_id=job_id)

    progress01 = _progress01(progress_raw)
    stage_progress = _progress_for_stage(progress_raw)
    stage = _safe_stage(last["step_code"] if last else None) or _infer_stage_from_progress(stage_progress)

    return MusicJobStatusOut(
        job_id=job_id,
        project_id=project_id,
        status=status_val or MusicJobStatus.queued.value,
        stage=stage,
        progress=progress01,
        tracks=[
            TrackItem(
                track_type=r["track_type"],
                artifact_id=r.get("artifact_id"),
                media_asset_id=r.get("media_asset_id"),
                duration_ms=r.get("duration_ms"),
                url=_track_url(r.get("meta_json")),
                content_type=_track_ct(r.get("meta_json")),
            )
            for r in track_rows
        ],
        error=str(error_val) if error_val else None,
        computed=computed,
        clip_manifest=clip_manifest,
    )


async def publish_project_to_video_or_fusion(*, job_id: UUID, user_id: UUID, publish_in: PublishMusicIn) -> Optional[PublishMusicOut]:
    jobs = MusicJobsRepo()
    projects = MusicProjectsRepo()
    tracks_repo = MusicTracksRepo()

    vrow = await jobs.get_music_video_job_row(job_id=job_id)

    if vrow:
        project_id = UUID(str(vrow["project_id"]))
        job_status = str(vrow.get("status") or "").strip()
        payload = _normalize_jsonb_payload(vrow.get("input_json"))
    else:
        job = await jobs.get_video_job(job_id=job_id)
        if not job:
            return None
        project_id = UUID(str(job["project_id"]))
        job_status = str(job.get("status") or "").strip()
        payload = _normalize_jsonb_payload(job.get("payload_json"))

    proj = await projects.get(project_id=project_id, user_id=user_id)
    if not proj:
        return None

    consent_dict = _as_dict(getattr(publish_in, "consent", None))
    if not _is_truthy(consent_dict.get("accepted")):
        return PublishMusicOut(status="error_consent_required", video_job_id=job_id, fusion_payload=None)

    target = str(getattr(publish_in, "target", "fusion") or "fusion").strip().lower()
    if target not in ("viewer", "fusion"):
        target = "fusion"

    if job_status == MusicJobStatus.failed.value:
        return PublishMusicOut(status="error_job_failed", video_job_id=job_id, fusion_payload=None)

    if job_status != MusicJobStatus.succeeded.value:
        return PublishMusicOut(status="error_job_not_ready", video_job_id=job_id, fusion_payload=None)

    hints = _as_dict(payload.get("provider_hints"))
    computed = _as_dict(payload.get("computed"))

    tracks = await tracks_repo.list_by_project(project_id=project_id)

    def find_track(tt: str):
        for t in tracks:
            if str(t.get("track_type") or "") == tt:
                return t
        return None

    full = find_track(MusicTrackType.full_mix.value)
    timed = find_track(MusicTrackType.timed_lyrics_json.value)

    if not full:
        return PublishMusicOut(status="error_missing_full_mix", video_job_id=job_id, fusion_payload=None)

    audio_url = (
        computed.get("audio_master_url")
        or computed.get("byo_audio_url")
        or computed.get("demo_audio_url")
        or _track_url(full.get("meta_json"))
    )
    audio_url = str(audio_url).strip() if audio_url else None

    if not audio_url:
        try:
            audio_url = await resolve_url_from_refs(
                user_id=user_id,
                media_asset_id=full.get("media_asset_id"),
                artifact_id=full.get("artifact_id"),
            )
        except Exception:
            audio_url = None

    if not audio_url:
        return PublishMusicOut(status="error_missing_full_mix_ref", video_job_id=job_id, fusion_payload=None)

    voice_ref_asset_id = None
    try:
        if proj.get("voice_ref_asset_id"):
            voice_ref_asset_id = str(proj["voice_ref_asset_id"])
        elif payload.get("voice_ref_asset_id"):
            voice_ref_asset_id = str(payload.get("voice_ref_asset_id"))
    except Exception:
        voice_ref_asset_id = None

    voice_ref_url = computed.get("voice_ref_url")
    if voice_ref_asset_id:
        try:
            vr_uuid = UUID(str(voice_ref_asset_id))
            fresh = await resolve_voice_ref_sas_url(project_id=project_id, user_id=user_id, voice_ref_asset_id=vr_uuid)
            if fresh:
                voice_ref_url = fresh
        except Exception:
            pass

    duration_ms = int(full.get("duration_ms") or 0)

    base_payload = {
        "project_id": str(project_id),
        "audio": {
            "track_type": MusicTrackType.full_mix.value,
            "artifact_id": str(full["artifact_id"]) if full.get("artifact_id") else None,
            "media_asset_id": str(full["media_asset_id"]) if full.get("media_asset_id") else None,
            "url": audio_url,
            "duration_ms": duration_ms,
            "content_type": _guess_audio_content_type(audio_url, default=_track_ct(full.get("meta_json")) or "audio/mpeg"),
        },
        "voice_reference": {"voice_ref_asset_id": voice_ref_asset_id, "url": voice_ref_url}
        if (voice_ref_asset_id or voice_ref_url)
        else None,
        "lyrics_text": computed.get("lyrics_text") or hints.get("lyrics_text") or hints.get("lyrics"),
        "timed_lyrics": {"artifact_id": str(timed["artifact_id"])} if timed and timed.get("artifact_id") else None,
        "timed_lyrics_inline": computed.get("timed_lyrics_json"),
        "duet_layout": proj["duet_layout"],
        "language_hint": proj.get("language_hint"),
        "target": target,
        "consent": consent_dict,
    }

    try:
        await persist_fusion_payload_best_effort(job_id=job_id, fusion_payload=base_payload)
    except Exception:
        pass

    # For viewer target, safely return the best rendered video URLs (does not hit svc-fusion).
    if target == "viewer":
        picked = _pick_best_video_urls(computed=computed)
        base_payload_viewer = dict(base_payload)
        base_payload_viewer["rendered_video"] = {
            "final_url": picked.get("final_url"),
            "preview_url": picked.get("preview_url"),
            "display_url": picked.get("display_url"),
            "source": picked.get("display_source"),
            "exports": picked.get("exports"),
        }
        return PublishMusicOut(status="published_viewer", video_job_id=job_id, fusion_payload=base_payload_viewer)

    # For fusion target, keep payload schema minimal/unchanged to avoid strict validation failures.
    return PublishMusicOut(status="published", video_job_id=job_id, fusion_payload=base_payload)