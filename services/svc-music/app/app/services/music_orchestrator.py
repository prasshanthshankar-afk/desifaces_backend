from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple
from uuid import UUID

from app.config import settings
from app.db import get_pool
from app.domain.enums import (
    MusicJobStage,
    MusicTrackType,
    MusicJobStatus,
    MusicProjectMode,
)
from app.domain.models import MusicJobStatusOut, TrackItem, PublishMusicIn, PublishMusicOut
from app.repos.music_jobs_repo import MusicJobsRepo
from app.repos.music_projects_repo import MusicProjectsRepo
from app.repos.music_tracks_repo import MusicTracksRepo
from app.repos.steps_repo import StepsRepo
from app.services.azure_storage_service import AzureStorageService

from .music_graph import MusicGraphState, MusicGraphTools, GraphTrack, run_video_pipeline

# Optional: keep orchestrator lean; planner lives elsewhere.
try:
    from app.services.music_planning.service import MusicPlanningService  # type: ignore
except Exception:
    MusicPlanningService = None  # type: ignore

# Optional: autopilot provider (Fal Sonauto v2) lives elsewhere to keep this file manageable.
# If module isn't present, orchestration still works via native fallback.
try:
    from app.services.music_providers.autopilot_router import (  # type: ignore
        AutopilotComposeResult,
        compose_full_mix_fal_sonauto_v2,
        default_autopilot_provider,
        normalize_provider,
    )
except Exception:
    AutopilotComposeResult = Any  # type: ignore

    def normalize_provider(p: Any) -> str:  # type: ignore
        return str(p or "").strip().lower().replace("-", "_")

    def default_autopilot_provider() -> str:  # type: ignore
        # If the external provider module isn't installed, do NOT advertise fal provider here.
        return "native"

    compose_full_mix_fal_sonauto_v2 = None  # type: ignore


# -----------------------------
# Queue / worker integration
# -----------------------------
async def enqueue_video_job(job_id: UUID) -> None:
    # DB polling worker: can be no-op. If you later add a real queue (Redis/ASB),
    # enqueue a message here.
    return None


# -----------------------------
# Helpers
# -----------------------------
def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        if s.startswith("{") or s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}
    return {}


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("[") or s.startswith("{"):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        return []
    return []


def _track_url(meta: Any) -> Optional[str]:
    # meta_json can be dict OR json-string depending on asyncpg codecs
    m = _as_dict(meta)
    return m.get("url") or m.get("byo_audio_url") or m.get("audio_master_url")


def _track_ct(meta: Any) -> Optional[str]:
    m = _as_dict(meta)
    return m.get("content_type") or m.get("mime")


def _is_truthy(x: Any) -> bool:
    if x is True:
        return True
    if x is False or x is None:
        return False
    if isinstance(x, (int, float)):
        return x != 0
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(x)


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _guess_audio_content_type(url: Optional[str], default: str = "audio/mpeg") -> str:
    if not url:
        return default
    s = str(url).split("?", 1)[0].lower()
    if s.endswith(".wav"):
        return "audio/wav"
    if s.endswith(".mp3"):
        return "audio/mpeg"
    if s.endswith(".m4a") or s.endswith(".mp4"):
        return "audio/mp4"
    if s.endswith(".aac"):
        return "audio/aac"
    if s.endswith(".ogg") or s.endswith(".opus"):
        return "audio/ogg"
    return default


def _fallback_music_plan(*, mode: str, language: str | None, hints: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tiny, deterministic fallback plan generator.
    Purpose: always provide a usable 'music_plan' even when MusicPlanningService is unavailable.
    Keep this compact + JSON-serializable (mobile can render this).
    """
    title = str(hints.get("title") or "Untitled").strip() or "Untitled"
    genre = str(hints.get("genre") or hints.get("genre_hint") or "pop").strip() or "pop"
    mood = str(hints.get("mood") or hints.get("vibe_hint") or "uplifting").strip() or "uplifting"
    tempo = str(hints.get("tempo") or "mid").strip() or "mid"
    style_refs = hints.get("style_refs") or hints.get("style_ref") or []

    # Normalize style refs into list[str]
    if isinstance(style_refs, str):
        s = style_refs.strip()
        if s and s.startswith("["):
            try:
                style_refs = json.loads(s)
            except Exception:
                style_refs = [style_refs]
        elif s:
            style_refs = [style_refs]
        else:
            style_refs = []
    if not isinstance(style_refs, list):
        style_refs = []

    # Simple mode-aware structure
    if str(mode).lower() == MusicProjectMode.byo.value:
        steps = [
            {"step": "ingest_audio", "why": "Use your uploaded track as the master audio"},
            {"step": "lyrics_strategy", "why": "Lyrics optional unless timed_lyrics_json requested"},
            {"step": "alignment_optional", "why": "If timed lyrics requested, align lyrics to audio"},
            {"step": "publish", "why": "Prepare payload for Viewer/Fusion"},
        ]
    else:
        steps = [
            {"step": "creative_brief", "why": "Lock title/genre/mood/tempo"},
            {"step": "lyrics", "why": "Generate or use provided lyrics"},
            {"step": "arrangement", "why": "Define sections (intro/verse/chorus/bridge/outro)"},
            {"step": "provider_route", "why": "Choose provider based on availability/constraints"},
            {"step": "generate_audio", "why": "Produce full mix + stems if requested"},
            {"step": "align_lyrics_optional", "why": "Generate timed_lyrics_json if requested"},
            {"step": "publish", "why": "Prepare payload for Viewer/Fusion"},
        ]

    summary = f"{title} — {genre}, {mood}, tempo {tempo} ({language or 'en'})"

    return {
        "version": 1,
        "source": "fallback",
        "summary": summary,
        "mode": str(mode),
        "language": language,
        "brief": {
            "title": title,
            "genre": genre,
            "mood": mood,
            "tempo": tempo,
            "style_refs": [str(x) for x in style_refs if str(x).strip()],
        },
        "steps": steps,
        "notes": [
            "This is a lightweight fallback plan.",
            "If MusicPlanningService is enabled, its plan will replace this.",
        ],
    }


def _safe_stage(val: str | None) -> Optional[MusicJobStage]:
    if not val:
        return None
    try:
        return MusicJobStage(val)
    except Exception:
        return None


def _infer_stage_from_progress(progress_0_100: int) -> MusicJobStage:
    p = int(progress_0_100 or 0)
    if p < 10:
        return MusicJobStage.intent
    if p < 25:
        return MusicJobStage.creative_brief
    if p < 35:
        return MusicJobStage.lyrics
    if p < 45:
        return MusicJobStage.arrangement
    if p < 60:
        return MusicJobStage.provider_route
    if p < 75:
        return MusicJobStage.generate_audio
    if p < 82:
        return MusicJobStage.align_lyrics
    if p < 90:
        return MusicJobStage.generate_performer_videos
    if p < 97:
        return MusicJobStage.compose_video
    return MusicJobStage.publish


def _progress01(raw: Any) -> float:
    """
    Normalize progress to [0..1].
    Accepts DB values either as 0..100 or 0..1.
    """
    try:
        p = float(raw or 0)
    except Exception:
        return 0.0
    if p <= 0:
        return 0.0
    if p > 1.0:
        return min(1.0, p / 100.0)
    return min(1.0, p)


def _progress_for_stage(raw: Any) -> int:
    """
    Normalize to 0..100 integer for stage inference.
    """
    return int(round(_progress01(raw) * 100))


def _normalize_mode(val: Any) -> str:
    # Fix: allow enum inputs without producing "MusicProjectMode.autopilot"
    v = getattr(val, "value", val)
    s = str(v or "").strip()
    if not s:
        return MusicProjectMode.autopilot.value
    return s.lower()


def _normalize_outputs(input_json: Dict[str, Any]) -> List[str]:
    """
    input_json originates from payload.model_dump(mode="json"), so enums become strings.
    outputs should be a list[str] of MusicTrackType values.
    """
    outs = _as_list(input_json.get("outputs"))
    out_strs: List[str] = []
    for x in outs:
        if x is None:
            continue
        v = str(x).strip().lower()
        if not v:
            continue
        try:
            MusicTrackType(v)
            out_strs.append(v)
        except Exception:
            continue

    if not out_strs:
        out_strs = [MusicTrackType.full_mix.value]

    seen = set()
    dedup: List[str] = []
    for o in out_strs:
        if o not in seen:
            seen.add(o)
            dedup.append(o)
    return dedup


def _outputs_set(outputs: List[str]) -> set[str]:
    return {str(x).strip().lower() for x in (outputs or []) if x}


def _get_byo_audio(
    hints: Dict[str, Any], input_json: Dict[str, Any] | None = None
) -> Tuple[Optional[str], Optional[int]]:
    """
    Standardize BYO audio hint keys across earlier experiments.
    IMPORTANT: This is the *actual song audio* (not voice reference).
    """
    ij = input_json or {}
    url = (
        ij.get("uploaded_audio_url")
        or ij.get("audio_master_url")
        or hints.get("byo_audio_url")
        or hints.get("uploaded_audio_url")
        or hints.get("audio_url")
        or hints.get("audio_master_url")
    )
    dur = (
        ij.get("uploaded_audio_duration_ms")
        or ij.get("audio_master_duration_ms")
        or hints.get("byo_duration_ms")
        or hints.get("duration_ms")
        or hints.get("audio_master_duration_ms")
    )
    try:
        dur_i = int(dur) if dur is not None else None
    except Exception:
        dur_i = None
    return (str(url) if url else None, dur_i)


def _maybe_import_alignment():
    """
    Try to use your real lyrics_alignment_service implementation if present.
    Fallback to a deterministic naive implementation if not.
    """
    try:
        import app.services.lyrics_alignment_service as las  # type: ignore
    except Exception:
        las = None

    real = getattr(las, "align_lyrics", None) if las else None
    naive = getattr(las, "naive_timed_lyrics", None) if las else None

    if naive is None:

        def naive_timed_lyrics_fallback(
            lyrics_text: str, duration_ms: int, *, language: str | None = None
        ) -> Dict[str, Any]:
            duration_ms = max(1, int(duration_ms or 1))
            lines = [ln.strip() for ln in (lyrics_text or "").splitlines()]
            lines = [ln for ln in lines if ln]
            if not lines:
                return {"version": 1, "language": language, "segments": []}

            n = len(lines)
            base = duration_ms // n
            rem = duration_ms % n
            t = 0
            segments: List[Dict[str, Any]] = []
            for i, line in enumerate(lines):
                seg_dur = base + (1 if i < rem else 0)
                start = t
                end = min(duration_ms, t + seg_dur)
                t = end
                words = [w for w in line.split(" ") if w]
                if not words:
                    segments.append({"start_ms": start, "end_ms": end, "text": line, "words": []})
                    continue
                wn = len(words)
                wbase = max(1, (end - start) // wn)
                wrem = (end - start) - (wbase * wn)
                wt = start
                witems = []
                for wi, w in enumerate(words):
                    wdur = wbase + (1 if wi < wrem else 0)
                    wstart = wt
                    wend = min(end, wt + wdur)
                    wt = wend
                    witems.append({"w": w, "start_ms": wstart, "end_ms": wend})
                segments.append({"start_ms": start, "end_ms": end, "text": line, "words": witems})

            if segments:
                segments[-1]["end_ms"] = duration_ms
                if segments[-1]["words"]:
                    segments[-1]["words"][-1]["end_ms"] = duration_ms

            return {"version": 1, "language": language, "segments": segments}

        naive = naive_timed_lyrics_fallback

    return real, naive


# -----------------------------
# studio_jobs envelope helpers (NEW / FIXED)
# -----------------------------
async def _table_exists(*, pool, regclass_text: str) -> bool:
    try:
        v = await pool.fetchval("select to_regclass($1)", str(regclass_text))
        return v is not None
    except Exception:
        return False


async def _get_table_columns(*, pool, schema: str, table: str) -> set[str]:
    try:
        rows = await pool.fetch(
            """
            select column_name
            from information_schema.columns
            where table_schema=$1 and table_name=$2
            """,
            schema,
            table,
        )
        return {str(r["column_name"]) for r in (rows or []) if r and r.get("column_name")}
    except Exception:
        return set()


def _stable_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(obj)


def _studio_request_hash(*, user_id: UUID, studio_type: str, job_id: UUID, payload_json: Dict[str, Any]) -> str:
    base = {
        "user_id": str(user_id),
        "studio_type": str(studio_type),
        "job_id": str(job_id),
        "payload": payload_json or {},
    }
    return hashlib.sha256(_stable_json(base).encode("utf-8")).hexdigest()


def _studio_type_candidates() -> List[str]:
    # Try common enum/check variants
    vals = ["music", "MUSIC", "music_studio", "MUSIC_STUDIO", "svc_music", "SVC_MUSIC"]
    out: List[str] = []
    seen = set()
    for v in vals:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _studio_status_candidates(status: str) -> List[str]:
    s = str(status or "").strip()
    vals = [s, s.lower(), s.upper()]
    out: List[str] = []
    seen = set()
    for v in vals:
        v = str(v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _ensure_studio_job_envelope(
    *,
    pool,
    job_id: UUID,
    user_id: UUID,
    project_id: UUID | None,
    status: str,
    input_json: Dict[str, Any] | None = None,
    meta_json: Dict[str, Any] | None = None,
) -> None:
    if not await _table_exists(pool, regclass_text="public.studio_jobs"):
        return

    try:
        exists = await pool.fetchval("select 1 from public.studio_jobs where id=$1 limit 1", job_id)
        if exists:
            return
    except Exception:
        pass

    cols = await _get_table_columns(pool, schema="public", table="studio_jobs")
    required = {"id", "studio_type", "status", "request_hash", "payload_json", "meta_json", "user_id"}
    if not required.issubset(cols):
        return

    payload = input_json if isinstance(input_json, dict) else {}
    meta: Dict[str, Any] = dict(meta_json or {})
    meta.setdefault("source", "svc-music")
    meta.setdefault("request_type", "music_video")
    if project_id:
        meta.setdefault("music_project_id", str(project_id))

    for stype in _studio_type_candidates():
        rh = _studio_request_hash(user_id=user_id, studio_type=stype, job_id=job_id, payload_json=payload)
        for st in _studio_status_candidates(status or "queued"):
            try:
                await pool.execute(
                    """
                    insert into public.studio_jobs(
                        id, studio_type, status, request_hash, payload_json, meta_json, user_id
                    )
                    values($1,$2,$3,$4,coalesce($5,'{}'::jsonb),coalesce($6,'{}'::jsonb),$7)
                    on conflict (id) do nothing
                    """,
                    job_id,
                    stype,
                    st,
                    rh,
                    payload,
                    meta,
                    user_id,
                )
                return
            except Exception:
                continue

    return


async def _update_studio_job_status_best_effort(
    *,
    pool,
    job_id: UUID,
    status: str,
    error_message: str | None = None,
    meta_patch: Dict[str, Any] | None = None,
) -> None:
    if not await _table_exists(pool=pool, regclass_text="public.studio_jobs"):
        return

    cols = await _get_table_columns(pool=pool, schema="public", table="studio_jobs")
    if not cols:
        return

    # Try a few status variants in case of enum/check constraints
    for st in _studio_status_candidates(status):
        sets: List[str] = []
        params: List[Any] = []

        def set_param(expr_col: str, val: Any) -> None:
            params.append(val)
            sets.append(f"{expr_col}=${len(params) + 1}")  # +1 because id is $1 in execute below

        if "status" in cols:
            set_param("status", st)

        if "updated_at" in cols:
            sets.append("updated_at=now()")

        if error_message:
            if "error_message" in cols:
                set_param("error_message", error_message)
            elif "error" in cols:
                set_param("error", error_message)

        if meta_patch and "meta_json" in cols:
            params.append(meta_patch)
            sets.append(f"meta_json=coalesce(meta_json,'{{}}'::jsonb) || ${len(params) + 1}::jsonb")

        if not sets:
            return

        try:
            await pool.execute(
                f"""
                update public.studio_jobs
                set {", ".join(sets)}
                where id=$1
                """,
                job_id,
                *params,
            )
            return
        except Exception:
            continue

    return


# -----------------------------
# Voice reference resolution (fresh SAS)
# -----------------------------
def _fallback_input_container() -> str:
    return (getattr(settings, "MUSIC_INPUT_CONTAINER", None) or "music-input").strip() or "music-input"


def _fallback_output_container() -> str:
    return (getattr(settings, "MUSIC_OUTPUT_CONTAINER", None) or "music-output").strip() or "music-output"


def _extract_container_and_path_from_meta(meta_json: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    meta_json may be TEXT or dict.
    Preferred new format:
      {"container": "music-input", "storage_path": "..."}
    Back-compat:
      {"storage_path": "..."}  (container missing)
    """
    m = _as_dict(meta_json)
    c = m.get("container") or m.get("blob_container")
    p = m.get("storage_path") or m.get("path")
    c = str(c).strip() if isinstance(c, str) and c.strip() else None
    p = str(p).strip() if isinstance(p, str) and p.strip() else None
    return c, p


async def _update_media_asset_refs_best_effort(
    *,
    pool,
    asset_id: UUID,
    new_storage_ref: str,
    container: Optional[str],
    storage_path: Optional[str],
) -> None:
    """
    media_assets.meta_json is JSONB.
    Merge container/storage_path into existing meta_json (best effort), then update storage_ref.
    """
    existing_meta: Any = {}
    try:
        r = await pool.fetchrow("select meta_json from public.media_assets where id=$1", asset_id)
        if r and r.get("meta_json") is not None:
            existing_meta = r["meta_json"]
    except Exception:
        existing_meta = {}

    meta_obj = _as_dict(existing_meta)

    if container and not meta_obj.get("container"):
        meta_obj["container"] = container
    if storage_path and not meta_obj.get("storage_path"):
        meta_obj["storage_path"] = storage_path

    try:
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, meta_json=$3, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
            meta_obj if meta_obj else {},
        )
    except Exception:
        # If meta_json update fails for any reason, still update storage_ref
        await pool.execute(
            """
            update public.media_assets
            set storage_ref=$2, updated_at=now()
            where id=$1
            """,
            asset_id,
            new_storage_ref,
        )


def _resolve_container_and_path(
    *, storage_ref: str | None, meta_json: Any
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Returns (container, blob_path, meta_container, url_container) for diagnostics / ordering.

    Resolution order:
      - container: meta_json.container, else URL container, else fallback input/output
      - path: meta_json.storage_path, else URL blob_path
    """
    meta_container, meta_path = _extract_container_and_path_from_meta(meta_json)

    url_container, url_path = (None, None)
    if storage_ref:
        url_container, url_path = AzureStorageService.parse_blob_url(storage_ref)

    blob_path = meta_path or url_path

    container = meta_container or url_container
    if not container and blob_path:
        container = _fallback_input_container()

    return container, blob_path, meta_container, url_container


async def _resolve_voice_ref_sas_url(
    *, project_id: UUID, user_id: UUID, voice_ref_asset_id: UUID | None
) -> Optional[str]:
    """
    Resolve a fresh SAS URL for voice reference (NOT song audio).
    """
    pool = await get_pool()

    if not voice_ref_asset_id:
        rowp = await pool.fetchrow(
            "select voice_ref_asset_id from public.music_projects where id=$1 and user_id=$2",
            project_id,
            user_id,
        )
        if not rowp or not rowp["voice_ref_asset_id"]:
            return None
        voice_ref_asset_id = UUID(str(rowp["voice_ref_asset_id"]))

    asset = await pool.fetchrow(
        """
        select id, storage_ref, meta_json
        from public.media_assets
        where id=$1 and user_id=$2
        limit 1
        """,
        voice_ref_asset_id,
        user_id,
    )
    if not asset:
        return None

    storage_ref = str(asset.get("storage_ref") or "") or None
    meta_json = asset.get("meta_json")

    container, blob_path, meta_container, url_container = _resolve_container_and_path(
        storage_ref=storage_ref,
        meta_json=meta_json,
    )

    if not blob_path:
        return storage_ref or None

    candidates: List[str] = []
    for c in (meta_container, url_container):
        if c and c not in candidates:
            candidates.append(c)
    for c in (_fallback_input_container(), _fallback_output_container()):
        if c and c not in candidates:
            candidates.append(c)

    last_err: Optional[Exception] = None
    for c in candidates:
        try:
            storage = AzureStorageService(container=c)
            refreshed = storage.sas_url_for(blob_path)

            await _update_media_asset_refs_best_effort(
                pool=pool,
                asset_id=voice_ref_asset_id,
                new_storage_ref=refreshed,
                container=c,
                storage_path=blob_path,
            )
            return refreshed
        except Exception as e:
            last_err = e
            continue

    _ = last_err
    return storage_ref or None


async def _resolve_url_from_refs(
    *, user_id: UUID, media_asset_id: UUID | None, artifact_id: UUID | None
) -> Optional[str]:
    """
    Best-effort: resolve a URL from media_assets.storage_ref or artifacts.storage_ref.
    """
    pool = await get_pool()
    if media_asset_id:
        try:
            r = await pool.fetchrow(
                "select storage_ref from public.media_assets where id=$1 and user_id=$2",
                media_asset_id,
                user_id,
            )
            if r and r.get("storage_ref"):
                return str(r["storage_ref"])
        except Exception:
            pass
    if artifact_id:
        for col in ("storage_ref", "url", "blob_url"):
            try:
                r = await pool.fetchrow(
                    f"select {col} from public.artifacts where id=$1 and user_id=$2",
                    artifact_id,
                    user_id,
                )
                if r and r.get(col):
                    return str(r[col])
            except Exception:
                continue
    return None


# -----------------------------
# Status + Publish (API helpers)
# -----------------------------
async def get_video_job_status(*, job_id: UUID, user_id: UUID) -> Optional[MusicJobStatusOut]:
    jobs = MusicJobsRepo()
    projects = MusicProjectsRepo()
    tracks_repo = MusicTracksRepo()
    steps = StepsRepo()

    job = await jobs.get_video_job(job_id=job_id)
    if not job:
        return None

    proj = await projects.get(project_id=job["project_id"], user_id=user_id)
    if not proj:
        return None

    track_rows = await tracks_repo.list_by_project(project_id=job["project_id"])

    last = await steps.latest_step(job_id=job_id)

    progress01 = _progress01(job.get("progress"))
    stage_progress = _progress_for_stage(job.get("progress"))
    stage = _safe_stage(last["step_code"] if last else None) or _infer_stage_from_progress(stage_progress)

    return MusicJobStatusOut(
        job_id=job["id"],
        project_id=job["project_id"],
        status=job["status"],
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
        error=job.get("error"),
    )


async def publish_project_to_video_or_fusion(
    *, job_id: UUID, user_id: UUID, publish_in: PublishMusicIn
) -> Optional[PublishMusicOut]:
    jobs = MusicJobsRepo()
    projects = MusicProjectsRepo()
    tracks_repo = MusicTracksRepo()

    job = await jobs.get_video_job(job_id=job_id)
    if not job:
        return None

    proj = await projects.get(project_id=job["project_id"], user_id=user_id)
    if not proj:
        return None

    if str(job["status"]) == MusicJobStatus.failed.value:
        return PublishMusicOut(status="error_job_failed", video_job_id=job_id, fusion_payload=None)

    if str(job["status"]) != MusicJobStatus.succeeded.value:
        return PublishMusicOut(status="error_job_not_ready", video_job_id=job_id, fusion_payload=None)

    input_json = _as_dict(job.get("input_json"))
    hints = _as_dict(input_json.get("provider_hints"))
    computed = _as_dict(input_json.get("computed"))

    audio_url, _ = _get_byo_audio(hints, input_json)
    if not audio_url:
        audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")

    tracks = await tracks_repo.list_by_project(project_id=job["project_id"])

    def find_track(tt: str):
        for t in tracks:
            if str(t.get("track_type") or "") == tt:
                return t
        return None

    full = find_track(MusicTrackType.full_mix.value)
    timed = find_track(MusicTrackType.timed_lyrics_json.value)

    if not full:
        return PublishMusicOut(status="error_missing_full_mix", video_job_id=job_id, fusion_payload=None)

    track_url = _track_url(full.get("meta_json"))
    if track_url:
        audio_url = track_url

    if not audio_url:
        try:
            audio_url = await _resolve_url_from_refs(
                user_id=user_id,
                media_asset_id=full.get("media_asset_id"),
                artifact_id=full.get("artifact_id"),
            )
        except Exception:
            audio_url = None

    has_audio_ref = bool(full.get("artifact_id") or full.get("media_asset_id") or audio_url)
    if not has_audio_ref:
        return PublishMusicOut(status="error_missing_full_mix_ref", video_job_id=job_id, fusion_payload=None)

    voice_ref_asset_id = None
    try:
        if proj.get("voice_ref_asset_id"):
            voice_ref_asset_id = str(proj["voice_ref_asset_id"])
        elif input_json.get("voice_ref_asset_id"):
            voice_ref_asset_id = str(input_json.get("voice_ref_asset_id"))
    except Exception:
        voice_ref_asset_id = None

    voice_ref_url = computed.get("voice_ref_url")
    if voice_ref_asset_id:
        try:
            vr_uuid = UUID(str(voice_ref_asset_id))
        except Exception:
            vr_uuid = None
        if vr_uuid:
            fresh = await _resolve_voice_ref_sas_url(
                project_id=UUID(str(proj["id"])),
                user_id=user_id,
                voice_ref_asset_id=vr_uuid,
            )
            if fresh:
                voice_ref_url = fresh

    base_payload = {
        "project_id": str(proj["id"]),
        "audio": {
            "track_type": MusicTrackType.full_mix.value,
            "artifact_id": str(full["artifact_id"]) if full.get("artifact_id") else None,
            "media_asset_id": str(full["media_asset_id"]) if full.get("media_asset_id") else None,
            "url": audio_url,
            "duration_ms": int(full.get("duration_ms") or 0),
        },
        "voice_reference": {"voice_ref_asset_id": voice_ref_asset_id, "url": voice_ref_url}
        if (voice_ref_asset_id or voice_ref_url)
        else None,
        "lyrics_text": computed.get("lyrics_text") or hints.get("lyrics_text") or hints.get("lyrics"),
        "timed_lyrics": {"artifact_id": str(timed["artifact_id"])} if timed and timed.get("artifact_id") else None,
        "timed_lyrics_inline": computed.get("timed_lyrics_json"),
        "duet_layout": proj["duet_layout"],
        "language_hint": proj.get("language_hint"),
        "target": publish_in.target,
        "consent": publish_in.consent,
    }

    if publish_in.target == "viewer":
        return PublishMusicOut(status="published_viewer", video_job_id=job_id, fusion_payload=base_payload)

    return PublishMusicOut(status="published", video_job_id=job_id, fusion_payload=base_payload)


# -----------------------------
# Tool implementation (pipeline actions)
# -----------------------------
class ConcreteMusicTools(MusicGraphTools):
    """
    Updated behavior to match modes:

    - autopilot/co_create:
        lyrics default generate (user doesn't need to type)
    - byo:
        lyrics default none (unless user uploads lyrics or timed_lyrics_json requested)
    - timed_lyrics_json requested:
        if lyrics missing, we auto-generate to allow alignment to succeed
    """

    def __init__(self, *, job_id: UUID, project_id: UUID, user_id: UUID, input_json: Dict[str, Any] | None = None):
        self.job_id = job_id
        self.project_id = project_id
        self.user_id = user_id
        self.input_json = input_json or {}

        self.hints = _as_dict(self.input_json.get("provider_hints"))
        self.quality = str(self.input_json.get("quality") or "standard")
        self.seed = self.input_json.get("seed")

        self._align_real, self._align_naive = _maybe_import_alignment()
        self._planner = MusicPlanningService() if MusicPlanningService else None

    def _demo_use_voice_ref_as_audio(self) -> bool:
        return _is_truthy(self.hints.get("demo_use_voice_ref_as_audio") or self.hints.get("demo_voice_ref_as_audio"))

    def _computed(self) -> Dict[str, Any]:
        return _as_dict(self.input_json.get("computed"))

    def _set_computed(self, key: str, value: Any) -> None:
        c = _as_dict(self.input_json.get("computed"))
        c[key] = value
        self.input_json["computed"] = c

    def _get_mode(self, s: MusicGraphState) -> str:
        return _normalize_mode(getattr(s, "mode", None))

    def _get_requested_outputs(self, s: MusicGraphState) -> set[str]:
        return _outputs_set(getattr(s, "requested_outputs", []) or [])

    def _pick_lyrics_source(self, *, mode: str, outputs: set[str], provided_lyrics: bool) -> str:
        src = ((self.input_json.get("lyrics_source") or self.hints.get("lyrics_source") or "").strip().lower())

        if provided_lyrics:
            return "upload"

        if src in ("generate", "upload", "none"):
            if src == "none" and MusicTrackType.timed_lyrics_json.value in outputs:
                return "generate"
            return src

        if mode == MusicProjectMode.byo.value:
            return "generate" if MusicTrackType.timed_lyrics_json.value in outputs else "none"

        return "generate"

    def _generate_fallback_lyrics(self, s: MusicGraphState) -> str:
        title = str(self.hints.get("title") or self.input_json.get("title") or "My Song").strip()
        mood = str(self.hints.get("mood") or self.hints.get("vibe_hint") or "uplifting").strip()
        genre = str(self.hints.get("genre") or self.hints.get("genre_hint") or "pop").strip()

        chorus = f"{title}, {title}\nWe rise with a {mood} glow\n{title}, {title}\nLet the whole world know"
        verse1 = (
            f"Verse 1:\nIn the {mood} night, we find our way\nOne small step, then we sway\n"
            f"Heartbeats sync to {genre} dreams\nNothing’s ever as it seems"
        )
        verse2 = (
            "Verse 2:\nHold the line, don’t let it fade\nMoments bright that we have made\n"
            "From today into the new\nI believe, and so do you"
        )
        bridge = "Bridge:\nBreathe in… breathe out…\nWe’re not alone, we’re here right now"

        return f"{verse1}\n\nChorus:\n{chorus}\n\n{verse2}\n\n{bridge}\n\nChorus:\n{chorus}\n"

    async def intent(self, s: MusicGraphState) -> Dict[str, Any]:
        await self.ensure_music_plan(s)
        return {
            "mode": getattr(s, "mode", None),
            "language_hint": getattr(s, "language_hint", None),
            "quality": self.quality,
            "seed": self.seed,
        }

    async def ensure_music_plan(self, s: MusicGraphState) -> None:
        computed = self._computed()
        force = _is_truthy(self.hints.get("force_replan") or self.input_json.get("force_replan"))
        if not force and computed.get("music_plan"):
            return

        mode = self._get_mode(s)
        language = getattr(s, "language_hint", None)

        plan_payload: Any = None
        if self._planner:
            plan_out = await self._planner.build_plan(
                mode=mode,
                language=language,
                hints=self.hints,
                computed=computed,
            )
            if hasattr(plan_out, "model_dump"):
                plan_payload = plan_out.model_dump(mode="json")  # type: ignore
            elif isinstance(plan_out, dict):
                plan_payload = plan_out
            else:
                plan_payload = {"summary": str(plan_out)}
        else:
            plan_payload = _fallback_music_plan(mode=mode, language=language, hints=self.hints)

        self._set_computed("music_plan", plan_payload)

        summary = _as_dict(plan_payload).get("summary")
        if summary:
            self._set_computed("plan_summary", summary)

    async def creative_brief(self, s: MusicGraphState) -> Dict[str, Any]:
        brief = {
            "title": self.hints.get("title"),
            "genre": self.hints.get("genre"),
            "mood": self.hints.get("mood"),
            "tempo": self.hints.get("tempo"),
            "style_refs": self.hints.get("style_refs"),
        }

        await self.ensure_music_plan(s)

        plan_summary = _as_dict(self._computed().get("music_plan")).get("summary") or self._computed().get("plan_summary")
        if plan_summary:
            brief["plan_summary"] = plan_summary

        return brief

    async def lyrics(self, s: MusicGraphState) -> Dict[str, Any]:
        mode = self._get_mode(s)
        outputs = self._get_requested_outputs(s)

        provided = (
            self.input_json.get("lyrics_text")
            or self.hints.get("lyrics_text")
            or self.hints.get("lyrics")
            or self._computed().get("lyrics_text")
        )
        provided_text = str(provided).strip() if provided else ""
        provided_lyrics = bool(provided_text)

        src = self._pick_lyrics_source(mode=mode, outputs=outputs, provided_lyrics=provided_lyrics)

        needs_lyrics = src in ("generate", "upload") or (MusicTrackType.timed_lyrics_json.value in outputs)
        if not needs_lyrics or src == "none":
            self._set_computed("lyrics_source_effective", "none")
            return {}

        if src == "upload" and not provided_text:
            if MusicTrackType.timed_lyrics_json.value in outputs:
                src = "generate"
            else:
                self._set_computed("lyrics_source_effective", "none")
                return {}

        if src == "generate" and not provided_text:
            provided_text = self._generate_fallback_lyrics(s)

        self._set_computed("lyrics_text", provided_text)
        self._set_computed("lyrics_source_effective", src)
        return {"lyrics_text": provided_text, "lyrics_source": src}

    async def arrangement(self, s: MusicGraphState) -> Dict[str, Any]:
        return {"arrangement_hint": self.hints.get("arrangement_hint")}

    async def route_provider(self, s: MusicGraphState) -> Dict[str, Any]:
        computed = self._computed()
        byo_url, _ = _get_byo_audio(self.hints, self.input_json)

        has_audio_master = bool(
            byo_url
            or computed.get("audio_master_url")
            or computed.get("byo_audio_url")
            or computed.get("demo_audio_url")
        )
        has_demo_voice_ref_audio = self._demo_use_voice_ref_as_audio() and bool(computed.get("voice_ref_url"))

        mode = self._get_mode(s)
        if mode == MusicProjectMode.byo.value or has_audio_master or has_demo_voice_ref_audio:
            return {"provider": "byo"}

        provider = (
            self.hints.get("music_provider")
            or self.hints.get("provider")
            or getattr(settings, "MUSIC_AUTOPILOT_PROVIDER", None)
        )
        provider = normalize_provider(provider) if provider else default_autopilot_provider()
        return {"provider": provider or "native"}

    def _ffmpeg_available(self) -> bool:
        return bool(shutil.which("ffmpeg"))

    def _write_silence_wav(self, *, path: Path, duration_ms: int, sample_rate: int = 44100) -> None:
        # Pure-python WAV fallback if ffmpeg is not available.
        duration_ms = max(1000, int(duration_ms or 30_000))
        frames = int(sample_rate * (duration_ms / 1000.0))
        silence = b"\x00\x00" * frames  # 16-bit mono silence
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(silence)

    async def _generate_native_fallback_full_mix(self, *, duration_ms: int) -> Tuple[str, int, str]:
        """
        Generate deterministic fallback audio and upload to Azure music-output.
        Prefers ffmpeg if available, otherwise uses pure-python wav writer.
        Returns (sas_url, duration_ms, content_type).
        """
        duration_ms = max(1000, int(duration_ms or 30_000))
        dur_s = max(1, int(round(duration_ms / 1000.0)))

        out_dir = Path("/tmp/df_music_native")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"fallback_full_mix_{int(time.time())}.wav"

        if self._ffmpeg_available():
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=44100:duration={dur_s}",
                "-c:a",
                "pcm_s16le",
                str(out_path),
            ]
            try:
                subprocess.run(cmd, check=True)
            except Exception:
                # If ffmpeg fails for any reason, fallback to pure-python wav.
                self._write_silence_wav(path=out_path, duration_ms=duration_ms)
        else:
            self._write_silence_wav(path=out_path, duration_ms=duration_ms)

        storage = AzureStorageService.for_output()
        sas_url = await storage.upload_music_fallback_audio_and_get_sas_url(
            user_id=str(self.user_id),
            project_id=str(self.project_id),
            job_id=str(self.job_id),
            local_path=out_path,
            content_type="audio/wav",
            blob_filename="fallback_full_mix.wav",
        )

        return (str(sas_url), duration_ms, "audio/wav")

    async def generate_audio(self, s: MusicGraphState) -> List[GraphTrack]:
        """
        Contract: MUST produce at least one 'full_mix' track (or raise).

        Separation:
          - audio_master_url/byo_audio_url = actual song audio
          - voice_ref_url = voice reference only
          - demo_use_voice_ref_as_audio=true can optionally use voice_ref_url as audio for demos
        """
        computed = self._computed()
        mode = self._get_mode(s)

        audio_url, audio_dur = _get_byo_audio(self.hints, self.input_json)

        if not audio_url:
            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")

        demo_audio_url = None
        if not audio_url and self._demo_use_voice_ref_as_audio():
            vr = computed.get("voice_ref_url")
            if vr:
                demo_audio_url = str(vr)

        final_audio_url = audio_url or demo_audio_url

        # ---- BYO (or already have master audio) ----
        if mode == MusicProjectMode.byo.value or final_audio_url:
            if not final_audio_url:
                raise Exception("missing_audio_master_url")

            if not audio_dur:
                audio_dur = int(
                    self.input_json.get("audio_master_duration_ms")
                    or self.hints.get("audio_master_duration_ms")
                    or computed.get("audio_master_duration_ms")
                    or 0
                ) or 30_000

            ct = _guess_audio_content_type(str(final_audio_url) if final_audio_url else None, "audio/mpeg")

            meta: Dict[str, Any] = {
                "audio_duration_ms": int(audio_dur),
                "url": str(final_audio_url),
                "content_type": ct,
                "audio_master_url": str(final_audio_url),
                "byo_audio_url": str(final_audio_url),
                "byo_duration_ms": int(audio_dur),
            }

            if demo_audio_url:
                meta.update(
                    {
                        "demo_audio_url": str(demo_audio_url),
                        "audio_source": "demo_voice_ref_url",
                        "is_demo": True,
                    }
                )
            else:
                meta.update(
                    {
                        "audio_source": "hints" if _get_byo_audio(self.hints, self.input_json)[0] else "computed",
                        "source": "byo",
                    }
                )

            return [
                GraphTrack(
                    track_type=MusicTrackType.full_mix.value,
                    duration_ms=int(audio_dur),
                    artifact_id=None,
                    media_asset_id=None,
                    meta=meta,
                )
            ]

        # ---- AUTOPILOT / CO_CREATE ----
        provider = normalize_provider(
            self.hints.get("music_provider")
            or self.hints.get("provider")
            or computed.get("audio_provider")
            or getattr(settings, "MUSIC_AUTOPILOT_PROVIDER", None)
            or default_autopilot_provider()
        )

        # 1) Try Fal Sonauto v2 if available/configured
        if provider in ("fal_sonauto_v2", "sonauto_v2", "sonauto") and callable(compose_full_mix_fal_sonauto_v2):
            try:
                seed_i: Optional[int] = None
                if self.seed is not None:
                    seed_i = int(float(self.seed))

                res: AutopilotComposeResult = await compose_full_mix_fal_sonauto_v2(  # type: ignore
                    user_id=str(self.user_id),
                    project_id=str(self.project_id),
                    job_id=str(self.job_id),
                    language_hint=getattr(s, "language_hint", None),
                    quality=str(self.quality or "standard"),
                    seed=seed_i,
                    hints=self.hints,
                    computed=self._computed(),
                )

                # Persist for downstream publish/status consistency
                self._set_computed("audio_provider", getattr(res, "provider", "fal_sonauto_v2"))
                self._set_computed("provider_request_id", getattr(res, "provider_request_id", None))
                self._set_computed("audio_master_url", getattr(res, "sas_url", None))
                self._set_computed("byo_audio_url", getattr(res, "sas_url", None))
                self._set_computed("audio_master_duration_ms", int(getattr(res, "duration_ms", 0) or 0))

                # If provider generated lyrics and user didn't provide lyrics, store them
                prov_lyrics = getattr(res, "lyrics", None)
                if prov_lyrics and not str(self._computed().get("lyrics_text") or "").strip():
                    self._set_computed("lyrics_text", str(prov_lyrics))
                    self._set_computed("lyrics_source_effective", "generate")

                meta2: Dict[str, Any] = {
                    "audio_duration_ms": int(getattr(res, "duration_ms", 0) or 0),
                    "url": str(getattr(res, "sas_url", "")),
                    "content_type": str(getattr(res, "content_type", "audio/mpeg") or "audio/mpeg"),
                    "audio_master_url": str(getattr(res, "sas_url", "")),
                    "byo_audio_url": str(getattr(res, "sas_url", "")),
                    "byo_duration_ms": int(getattr(res, "duration_ms", 0) or 0),
                    "audio_source": "autopilot_provider",
                    "provider": str(getattr(res, "provider", "fal_sonauto_v2")),
                    "provider_request_id": getattr(res, "provider_request_id", None),
                    "provider_seed": int(getattr(res, "provider_seed", 0) or 0),
                    "provider_tags": list(getattr(res, "tags", []) or []),
                    "source_url": getattr(res, "source_url", None),
                    "is_demo": False,
                    "source": "autopilot",
                }

                dur_ms = int(getattr(res, "duration_ms", 0) or 0) or 30_000
                return [
                    GraphTrack(
                        track_type=MusicTrackType.full_mix.value,
                        duration_ms=dur_ms,
                        artifact_id=None,
                        media_asset_id=None,
                        meta=meta2,
                    )
                ]
            except Exception as e:
                # Never break UX: record the failure and continue with native fallback
                self._set_computed("autopilot_provider_error", str(e))

        # 2) Always-works native fallback so pipeline never fails
        plan = _as_dict(self._computed().get("music_plan"))
        duration_ms = _coerce_int(
            plan.get("duration_ms")
            or self.input_json.get("duration_ms")
            or self.hints.get("duration_ms")
            or self._computed().get("audio_master_duration_ms")
            or 30_000,
            30_000,
        )

        fallback_url, fallback_dur_ms, ct = await self._generate_native_fallback_full_mix(duration_ms=duration_ms)

        meta2: Dict[str, Any] = {
            "audio_duration_ms": int(fallback_dur_ms),
            "url": fallback_url,
            "content_type": ct,
            "audio_master_url": fallback_url,
            "byo_audio_url": fallback_url,
            "byo_duration_ms": int(fallback_dur_ms),
            "audio_source": "fallback_native",
            "provider": "native",
            "source": "autopilot",
            "is_demo": True,
        }

        return [
            GraphTrack(
                track_type=MusicTrackType.full_mix.value,
                duration_ms=int(fallback_dur_ms),
                artifact_id=None,
                media_asset_id=None,
                meta=meta2,
            )
        ]

    async def align_lyrics(self, s: MusicGraphState) -> Optional[GraphTrack]:
        outputs = self._get_requested_outputs(s)
        if MusicTrackType.timed_lyrics_json.value not in outputs:
            return None

        computed = self._computed()
        lyrics_text = str(
            computed.get("lyrics_text") or self.hints.get("lyrics_text") or self.hints.get("lyrics") or ""
        ).strip()
        if not lyrics_text:
            return None

        dur = 0
        for t in getattr(s, "tracks", []) or []:
            if str(getattr(t, "track_type", "")) == MusicTrackType.full_mix.value:
                dur = int(getattr(t, "duration_ms", 0) or 0)
                break
        if dur <= 0:
            return None

        audio_url, _ = _get_byo_audio(self.hints, self.input_json)
        if not audio_url:
            audio_url = computed.get("audio_master_url") or computed.get("byo_audio_url") or computed.get("demo_audio_url")
        if not audio_url and self._demo_use_voice_ref_as_audio():
            audio_url = computed.get("voice_ref_url")

        timed: Dict[str, Any] | None = None
        try:
            if audio_url and callable(self._align_real):
                try:
                    timed = await self._align_real(
                        audio_url=audio_url,
                        lyrics_text=lyrics_text,
                        language=getattr(s, "language_hint", None),
                    )  # type: ignore
                except TypeError:
                    timed = await self._align_real(audio_url, lyrics_text, getattr(s, "language_hint", None))  # type: ignore

            if timed is None and callable(self._align_naive):
                timed = self._align_naive(lyrics_text, dur, language=getattr(s, "language_hint", None))  # type: ignore
        except Exception:
            timed = None

        if not timed:
            return None

        return GraphTrack(
            track_type=MusicTrackType.timed_lyrics_json.value,
            duration_ms=0,
            artifact_id=None,
            media_asset_id=None,
            meta={"inline_json": timed},
        )

    async def generate_performer_videos(self, s: MusicGraphState) -> Dict[str, Any]:
        render_video = bool(self.hints.get("render_video") or self.hints.get("generate_video"))
        if not render_video:
            return {"skipped": True}
        return {"skipped": True, "reason": "performer_video_not_implemented"}

    async def compose_video(self, s: MusicGraphState) -> Dict[str, Any]:
        render_video = bool(self.hints.get("render_video") or self.hints.get("generate_video"))
        if not render_video:
            return {"skipped": True}
        return {"skipped": True, "reason": "compose_not_implemented"}

    async def qc(self, s: MusicGraphState) -> Dict[str, Any]:
        have_full = any(
            str(getattr(t, "track_type", "")) == MusicTrackType.full_mix.value for t in getattr(s, "tracks", []) or []
        )
        if not have_full:
            raise Exception("qc_failed_missing_full_mix")
        return {"ok": True}


# -----------------------------
# Worker entrypoint
# -----------------------------
async def run_music_video_job(job_id: UUID) -> None:
    jobs = MusicJobsRepo()
    tracks_repo = MusicTracksRepo()
    steps = StepsRepo()

    job = await jobs.get_video_job(job_id=job_id)
    if not job:
        return

    pool = await get_pool()
    proj_row = await pool.fetchrow("select * from music_projects where id=$1", job["project_id"])
    if not proj_row:
        # keep existing behavior
        await jobs.set_video_job_failed(job_id=job_id, error="project_not_found")
        return
    proj = dict(proj_row)

    # ✅ ALWAYS ensure studio_jobs envelope (even if job already succeeded/failed)
    try:
        current_status = str(job.get("status") or "queued")
        await _ensure_studio_job_envelope(
            pool=pool,
            job_id=job_id,
            user_id=UUID(str(proj["user_id"])),
            project_id=UUID(str(proj["id"])),
            status=current_status,
            input_json=_as_dict(job.get("input_json")),
            meta_json={"source": "svc-music", "music_project_id": str(proj["id"]), "request_type": "music_video"},
        )
        await _update_studio_job_status_best_effort(
            pool=pool,
            job_id=job_id,
            status=current_status,
            meta_patch={"svc": "svc-music", "music_project_id": str(proj["id"])},
        )
    except Exception:
        pass

    #  NOW we can safely early-return
    if job["status"] in (MusicJobStatus.succeeded.value, MusicJobStatus.failed.value):
        return

    input_json = _as_dict(job.get("input_json"))
    computed_pre = _as_dict(input_json.get("computed"))

    vr_raw = input_json.get("voice_ref_asset_id") or proj.get("voice_ref_asset_id")
    try:
        vr_id = UUID(str(vr_raw)) if vr_raw else None
    except Exception:
        vr_id = None

    voice_ref_url = await _resolve_voice_ref_sas_url(
        project_id=UUID(str(proj["id"])),
        user_id=UUID(str(proj["user_id"])),
        voice_ref_asset_id=vr_id,
    )

    if computed_pre.get("voice_ref_url") != voice_ref_url:
        computed_pre["voice_ref_url"] = voice_ref_url
        input_json["computed"] = computed_pre
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)

    try:
        computed_before = json.loads(json.dumps(_as_dict(input_json.get("computed"))))
    except Exception:
        computed_before = dict(_as_dict(input_json.get("computed")))

    requested_outputs = _normalize_outputs(input_json)

    state = MusicGraphState(
        job_id=job_id,
        project_id=proj["id"],
        user_id=proj["user_id"],
        mode=_normalize_mode(proj.get("mode")),
        duet_layout=str(proj.get("duet_layout") or "split_screen").lower(),
        language_hint=proj.get("language_hint") or "en-IN",
        scene_pack_id=proj.get("scene_pack_id"),
        camera_edit=str(proj.get("camera_edit") or "beat_cut").lower(),
        band_pack=proj.get("band_pack") or [],
        requested_outputs=requested_outputs,
    )

    tools = ConcreteMusicTools(
        job_id=job_id,
        project_id=state.project_id,
        user_id=state.user_id,
        input_json=input_json,
    )

    try:
        state = await run_video_pipeline(state, tools, jobs=jobs, steps=steps)

        computed = _as_dict(input_json.get("computed"))

        for t in state.tracks:
            tt = str(getattr(t, "track_type", ""))
            meta = getattr(t, "meta", None)

            if tt == MusicTrackType.full_mix.value and isinstance(meta, dict):
                am = meta.get("audio_master_url")
                if am:
                    computed["audio_master_url"] = am
                    computed["byo_audio_url"] = am

                demo = meta.get("demo_audio_url")
                if demo:
                    computed["demo_audio_url"] = demo

                dur = meta.get("audio_duration_ms") or meta.get("byo_duration_ms")
                if dur:
                    try:
                        computed["audio_master_duration_ms"] = int(dur)
                    except Exception:
                        pass

            if tt == MusicTrackType.timed_lyrics_json.value and isinstance(meta, dict):
                inline = meta.get("inline_json")
                if inline:
                    computed["timed_lyrics_json"] = inline

        tool_computed = tools._computed()
        for k in (
            "lyrics_text",
            "lyrics_source_effective",
            "music_plan",
            "plan_summary",
            "voice_ref_url",
            "audio_provider",
            "provider_request_id",
            "autopilot_provider_error",
        ):
            if k in tool_computed and tool_computed.get(k) is not None:
                computed[k] = tool_computed.get(k)

        changed = computed != computed_before
        if changed:
            input_json["computed"] = computed
            await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)

        for t in state.tracks:
            await tracks_repo.upsert_track(
                project_id=state.project_id,
                track_type=t.track_type,
                duration_ms=int(t.duration_ms or 0),
                artifact_id=t.artifact_id,
                media_asset_id=t.media_asset_id,
                meta_json=(t.meta if isinstance(t.meta, dict) else None),
            )

        await jobs.set_video_job_succeeded(
            job_id=job_id,
            preview_video_asset_id=state.preview_video_asset_id,
            final_video_asset_id=state.final_video_asset_id,
            performer_a_video_asset_id=state.performer_a_video_asset_id,
            performer_b_video_asset_id=state.performer_b_video_asset_id,
        )

        # ---- Best-effort envelope status update for dashboard ----
        try:
            await _update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="succeeded",
                meta_patch={"music_project_id": str(proj["id"]), "svc": "svc-music"},
            )
        except Exception:
            pass

    except Exception as e:
        # Fix: use meta_json (consistent with music_graph.py usage) to avoid signature mismatch bugs.
        try:
            await steps.upsert_step(
                job_id=job_id,
                step_code="failed",
                status="failed",
                meta_json={"error": str(e)},
            )
        except Exception:
            pass

        await jobs.set_video_job_failed(job_id=job_id, error=str(e))

        # ---- Best-effort envelope status update for dashboard ----
        try:
            await _update_studio_job_status_best_effort(
                pool=pool,
                job_id=job_id,
                status="failed",
                error_message=str(e),
                meta_patch={"music_project_id": str(proj["id"]), "svc": "svc-music"},
            )
        except Exception:
            pass


async def run_compose_job(job_id: UUID) -> None:
    await run_music_video_job(job_id)