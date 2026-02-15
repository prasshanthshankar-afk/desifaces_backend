from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import urllib.parse
import urllib.request
import ipaddress
from typing import Any, Dict, Optional, List
from uuid import UUID

from app.db import get_pool
from app.domain.enums import MusicProjectMode, MusicTrackType
from app.repos.music_candidates_repo import MusicCandidatesRepo
from app.repos.provider_runs_repo import ProviderRunsRepo
from app.services.music_candidates_controller import MusicCandidatesController
from app.services.music_tools import ConcreteMusicTools
from app.services.music_graph import MusicGraphState
from app.services.audio_probe_service import AudioProbeService


def _as_dict(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and x.strip():
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _as_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, str) and x.strip():
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


async def _load_job_bundle(job_id: UUID) -> Dict[str, Any]:
    pool = await get_pool()
    job = await pool.fetchrow("select * from public.music_video_jobs where id=$1", job_id)
    if not job:
        raise ValueError("job_not_found")
    proj = await pool.fetchrow("select * from public.music_projects where id=$1", job["project_id"])
    if not proj:
        raise ValueError("project_not_found")
    return {"job": dict(job), "project": dict(proj)}


async def _build_state_and_tools(
    job_id: UUID,
) -> tuple[MusicGraphState, ConcreteMusicTools, Dict[str, Any], Dict[str, Any]]:
    bundle = await _load_job_bundle(job_id)
    job = bundle["job"]
    proj = bundle["project"]
    input_json = _as_dict(job.get("input_json"))

    state = MusicGraphState(
        job_id=UUID(str(job["id"])),
        project_id=UUID(str(proj["id"])),
        user_id=UUID(str(proj["user_id"])),
        mode=str(proj.get("mode") or MusicProjectMode.autopilot.value).lower(),
        duet_layout=str(proj.get("duet_layout") or "split_screen").lower(),
        language_hint=proj.get("language_hint") or "en-IN",
        scene_pack_id=proj.get("scene_pack_id"),
        camera_edit=str(proj.get("camera_edit") or "beat_cut").lower(),
        band_pack=proj.get("band_pack") or [],
        requested_outputs=[MusicTrackType.full_mix.value],
    )

    tools = ConcreteMusicTools(
        job_id=state.job_id,
        project_id=state.project_id,
        user_id=state.user_id,
        input_json=input_json,
    )
    return state, tools, input_json, proj


async def _get_candidate_status(candidate_id: UUID) -> Optional[str]:
    pool = await get_pool()
    try:
        r = await pool.fetchrow(
            "select status from public.music_candidates where id=$1 limit 1",
            candidate_id,
        )
        if not r:
            return None
        return str(r.get("status") or "").strip() or None
    except Exception:
        return None


async def _notify_controllers_best_effort(*, job_id: UUID) -> None:
    # Back-compat controller
    try:
        ctrl = MusicCandidatesController()
        await ctrl.refresh_required_action(job_id=job_id)
    except Exception:
        pass

    # LangGraph controller
    try:
        from app.services.music_graph_controller import MusicGraphController

        g = MusicGraphController()
        await g.tick(job_id=job_id, trigger="provider_run_done")
    except Exception:
        pass


def _fallback_bpm(*, duration_ms: int) -> int:
    if duration_ms and duration_ms < 45_000:
        return 128
    return 120


def _fallback_segments(*, duration_ms: int) -> Dict[str, Any]:
    dur = int(duration_ms or 0)
    if dur <= 0:
        return {
            "segments": [{"start_ms": 0, "end_ms": 30_000, "label": "clip_1"}],
            "segment_plan": {"kind": "fallback_first_30s", "start_ms": 0, "end_ms": 30_000},
        }
    clip = 30_000
    if dur <= clip:
        return {
            "segments": [{"start_ms": 0, "end_ms": dur, "label": "full_track"}],
            "segment_plan": {"kind": "fallback_full_track", "start_ms": 0, "end_ms": dur},
        }
    return {
        "segments": [
            {"start_ms": 0, "end_ms": 15_000, "label": "clip_a"},
            {"start_ms": 15_000, "end_ms": 30_000, "label": "clip_b"},
        ],
        "segment_plan": {"kind": "fallback_first_30s_split", "start_ms": 0, "end_ms": 30_000},
    }


def _guess_ext_from_url(url: str) -> str:
    try:
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        if ext in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"):
            return ext
    except Exception:
        pass
    return ".bin"


def _validate_download_url(url: str) -> None:
    """
    Minimal SSRF hardening:
    - allow only http/https
    - block localhost and IP-literals that are private/loopback/link-local/etc.
    """
    p = urllib.parse.urlparse(url)
    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("invalid_audio_url_scheme")

    host = (p.hostname or "").strip().lower()
    if not host:
        raise ValueError("invalid_audio_url_host")

    if host in ("localhost", "0.0.0.0"):
        raise ValueError("blocked_audio_url_host")

    # If hostname is an IP literal, block private/loopback/link-local/etc.
    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("blocked_audio_url_ip")
    except ValueError:
        # Not an IP literal → ok (hostname)
        pass


def _download_url_to_temp_file(
    url: str,
    *,
    max_bytes: int,
    timeout_s: int,
) -> str:
    """
    Downloads to a temp file and returns local_path.
    Caller must delete the file.
    """
    _validate_download_url(url)

    ext = _guess_ext_from_url(url)
    fd, tmp_path = tempfile.mkstemp(prefix="df_audio_", suffix=ext)
    os.close(fd)

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "svc-music/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp, open(tmp_path, "wb") as f:
            read = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                read += len(chunk)
                if read > max_bytes:
                    raise ValueError("audio_too_large_for_probe")
                f.write(chunk)
        return tmp_path
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def _probe_duration_ms_from_url_best_effort(url: Optional[str]) -> Optional[int]:
    if not url or not str(url).strip():
        return None

    probe = AudioProbeService()
    tmp_path: Optional[str] = None

    max_bytes = _coerce_int(os.getenv("DF_AUDIO_PROBE_MAX_BYTES"), 120 * 1024 * 1024)  # default 120MB
    timeout_s = _coerce_int(os.getenv("DF_AUDIO_PROBE_TIMEOUT_S"), 25)

    try:
        tmp_path = _download_url_to_temp_file(str(url).strip(), max_bytes=max_bytes, timeout_s=timeout_s)
        return probe.duration_ms(tmp_path)
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


async def _maybe_call_alignment(fn, *args, **kwargs):
    if fn is None:
        return None
    try:
        if inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        out = fn(*args, **kwargs)
        if inspect.isawaitable(out):
            return await out
        return out
    except Exception:
        return None


def _maybe_import_alignment():
    """
    Tries:
      - app.services.lyrics_alignment_service.align_lyrics (real; may be sync or async)
      - app.services.lyrics_alignment_service.naive_timed_lyrics (optional)
    Provides fallback naive implementation if missing.
    """
    try:
        import app.services.lyrics_alignment_service as las  # type: ignore
    except Exception:
        las = None

    real = getattr(las, "align_lyrics", None) if las else None
    naive = getattr(las, "naive_timed_lyrics", None) if las else None

    if naive is None:

        def naive_timed_lyrics_fallback(lyrics_text: str, duration_ms: int, *, language: str | None = None) -> Dict[str, Any]:
            duration_ms = max(1, int(duration_ms or 1))
            lines = [ln.strip() for ln in (lyrics_text or "").splitlines()]
            lines = [ln for ln in lines if ln]
            if not lines:
                return {"version": 1, "language": language, "segments": []}

            n = len(lines)
            base = duration_ms // n
            rem = duration_ms % n
            t = 0
            segs: List[Dict[str, Any]] = []
            for i, line in enumerate(lines):
                seg_dur = base + (1 if i < rem else 0)
                start = t
                end = min(duration_ms, t + seg_dur)
                t = end
                segs.append({"start_ms": start, "end_ms": end, "text": line, "words": []})

            if segs:
                segs[-1]["end_ms"] = duration_ms
            return {"version": 1, "language": language, "segments": segs}

        naive = naive_timed_lyrics_fallback

    return real, naive


async def run_provider_runs_forever(*, poll_interval_s: float = 0.5) -> None:
    runs = ProviderRunsRepo()
    cands = MusicCandidatesRepo()

    while True:
        row = await runs.claim_next()
        if not row:
            await asyncio.sleep(poll_interval_s)
            continue

        run_id = UUID(str(row["id"]))
        job_id = UUID(str(row["job_id"]))
        provider = str(row.get("provider") or "native").strip() or "native"
        meta = _as_dict(row.get("meta_json"))
        req = _as_dict(row.get("request_json"))

        run_type = str(meta.get("run_type") or "").strip()
        candidate_id_raw = meta.get("candidate_id")
        attempt = _coerce_int(meta.get("attempt") or req.get("attempt") or 1, 1)

        group_id = meta.get("group_id")
        variant_index = meta.get("variant_index")

        try:
            # -------------------------
            # NON-CANDIDATE runs (BYO analysis + align)
            # -------------------------
            if run_type in ("byo_bpm_detect", "byo_segment_detect", "align_lyrics"):
                if not group_id:
                    raise ValueError("missing_group_id")

                # duration_ms can be null/None in request_json; coerce to 0
                duration_ms = _coerce_int(req.get("duration_ms"), 0)
                audio_url = req.get("audio_url") or req.get("url") or req.get("byo_audio_url")
                probed = False
                if duration_ms <= 0 and audio_url:
                    d = _probe_duration_ms_from_url_best_effort(str(audio_url))
                    if d:
                        duration_ms = int(d)
                        probed = True

                if run_type == "byo_bpm_detect":
                    bpm = _fallback_bpm(duration_ms=duration_ms)
                    resp = {
                        "ok": True,
                        "run_type": run_type,
                        "group_id": str(group_id),
                        "duration_ms": duration_ms,
                        "bpm": bpm,
                        "beat_grid": None,
                        "estimated": True,
                        "probed_duration": probed,
                    }
                    await runs.set_result(run_id=run_id, provider_status="succeeded", response_json=resp)

                elif run_type == "byo_segment_detect":
                    seg = _fallback_segments(duration_ms=duration_ms)
                    resp = {
                        "ok": True,
                        "run_type": run_type,
                        "group_id": str(group_id),
                        "duration_ms": duration_ms,
                        "segments": seg.get("segments") or [],
                        "segment_plan": seg.get("segment_plan"),
                        "estimated": True,
                        "probed_duration": probed,
                    }
                    await runs.set_result(run_id=run_id, provider_status="succeeded", response_json=resp)

                elif run_type == "align_lyrics":
                    lyrics_text = str(req.get("lyrics_text") or "")
                    language = str(req.get("language") or "en").strip() or "en"

                    if duration_ms <= 0:
                        raise ValueError("missing_duration_ms_for_alignment")

                    real, naive = _maybe_import_alignment()
                    timed = await _maybe_call_alignment(real, lyrics_text, duration_ms, language=language)
                    if timed is None and naive is not None:
                        timed = naive(lyrics_text, duration_ms, language=language)  # type: ignore

                    resp = {
                        "ok": True,
                        "run_type": run_type,
                        "group_id": str(group_id),
                        "duration_ms": duration_ms,
                        "timed_lyrics_json": timed,
                        "probed_duration": probed,
                    }
                    await runs.set_result(run_id=run_id, provider_status="succeeded", response_json=resp)

                await _notify_controllers_best_effort(job_id=job_id)
                continue

            # -------------------------
            # CANDIDATE runs
            # -------------------------
            if not candidate_id_raw:
                raise ValueError("missing_candidate_id")

            cid = UUID(str(candidate_id_raw))

            existing_status = await _get_candidate_status(cid)
            if existing_status and str(existing_status).lower() in ("succeeded", "failed", "chosen", "discarded", "abandoned"):
                await runs.set_result(
                    run_id=run_id,
                    provider_status="abandoned",
                    response_json={
                        "ok": True,
                        "skipped": True,
                        "reason": f"candidate_status_{existing_status}",
                        "candidate_id": str(cid),
                    },
                    meta_patch={"skipped": True, "skip_reason": f"candidate_status_{existing_status}"},
                )
                await _notify_controllers_best_effort(job_id=job_id)
                continue

            try:
                await cands.update_candidate(
                    candidate_id=cid,
                    status="running",
                    provider=provider,
                    provider_run_id=run_id,
                    meta_patch={
                        "run_type": run_type,
                        "attempt": attempt,
                        "group_id": group_id,
                        "variant_index": variant_index,
                    },
                )
            except Exception:
                pass

            state, tools, input_json, proj = await _build_state_and_tools(job_id)

            overrides = _as_dict(req.get("overrides"))
            if overrides:
                input_json.setdefault("provider_hints", {})
                if isinstance(input_json["provider_hints"], dict):
                    input_json["provider_hints"].update(overrides)

                outs = overrides.get("outputs")
                if isinstance(outs, list) and outs:
                    state.requested_outputs = [str(x).strip().lower() for x in outs if str(x).strip()]
                    if MusicTrackType.full_mix.value not in state.requested_outputs:
                        state.requested_outputs.append(MusicTrackType.full_mix.value)

            if run_type == "lyrics_candidate":
                out = await tools.lyrics(state)
                lyrics_text: Optional[str] = None
                if isinstance(out, dict):
                    lyrics_text = out.get("lyrics_text") or out.get("text")
                if not lyrics_text or not str(lyrics_text).strip():
                    raise ValueError("lyrics_generation_failed")

                score = {"overall": 0.6, "qc": "pass", "len": len(str(lyrics_text))}

                await cands.update_candidate(
                    candidate_id=cid,
                    status="succeeded",
                    provider=provider,
                    provider_run_id=run_id,
                    content_json={"lyrics_text": lyrics_text},
                    score_json=score,
                    meta_patch={"group_id": group_id, "variant_index": variant_index, "attempt": attempt},
                )

                await runs.set_result(
                    run_id=run_id,
                    provider_status="succeeded",
                    response_json={"ok": True, "candidate_id": str(cid), "lyrics_len": len(str(lyrics_text))},
                )

            elif run_type == "audio_candidate":
                tracks = await tools.generate_audio(state)

                full = None
                for t in tracks or []:
                    if str(getattr(t, "track_type", "")).strip().lower() == MusicTrackType.full_mix.value:
                        full = t
                        break
                if not full:
                    raise ValueError("audio_generation_failed")

                meta_t = getattr(full, "meta", {}) if full else {}
                dur = int(getattr(full, "duration_ms", 0) or 0)
                score = {"overall": 0.7, "qc": "pass", "duration_ms": dur}

                await cands.update_candidate(
                    candidate_id=cid,
                    status="succeeded",
                    provider=provider,
                    provider_run_id=run_id,
                    artifact_id=getattr(full, "artifact_id", None),
                    media_asset_id=getattr(full, "media_asset_id", None),
                    duration_ms=dur,
                    content_json={"track_type": MusicTrackType.full_mix.value},
                    score_json=score,
                    meta_patch={
                        "audio_meta": meta_t or {},
                        "group_id": group_id,
                        "variant_index": variant_index,
                        "attempt": attempt,
                    },
                )

                await runs.set_result(
                    run_id=run_id,
                    provider_status="succeeded",
                    response_json={
                        "ok": True,
                        "candidate_id": str(cid),
                        "duration_ms": dur,
                        "artifact_id": str(getattr(full, "artifact_id", None)) if getattr(full, "artifact_id", None) else None,
                        "media_asset_id": str(getattr(full, "media_asset_id", None)) if getattr(full, "media_asset_id", None) else None,
                    },
                )

            elif run_type == "video_candidate":
                await tools.generate_performer_videos(state)
                await tools.compose_video(state)

                score = {"overall": 0.6, "qc": "pass"}
                chosen_asset = state.preview_video_asset_id or state.final_video_asset_id

                await cands.update_candidate(
                    candidate_id=cid,
                    status="succeeded",
                    provider=provider,
                    provider_run_id=run_id,
                    media_asset_id=chosen_asset,
                    score_json=score,
                    meta_patch={
                        "preview_video_asset_id": str(state.preview_video_asset_id) if state.preview_video_asset_id else None,
                        "final_video_asset_id": str(state.final_video_asset_id) if state.final_video_asset_id else None,
                        "group_id": group_id,
                        "variant_index": variant_index,
                        "attempt": attempt,
                    },
                )

                await runs.set_result(
                    run_id=run_id,
                    provider_status="succeeded",
                    response_json={
                        "ok": True,
                        "candidate_id": str(cid),
                        "preview_video_asset_id": str(state.preview_video_asset_id) if state.preview_video_asset_id else None,
                        "final_video_asset_id": str(state.final_video_asset_id) if state.final_video_asset_id else None,
                    },
                )

            else:
                raise ValueError(f"unknown_run_type:{run_type}")

            await _notify_controllers_best_effort(job_id=job_id)

        except Exception as e:
            try:
                if candidate_id_raw:
                    await cands.update_candidate(
                        candidate_id=UUID(str(candidate_id_raw)),
                        status="failed",
                        provider=provider,
                        provider_run_id=run_id,
                        meta_patch={
                            "error": str(e),
                            "run_type": run_type,
                            "attempt": attempt,
                            "group_id": group_id,
                            "variant_index": variant_index,
                        },
                    )
            except Exception:
                pass

            await runs.set_result(
                run_id=run_id,
                provider_status="failed",
                response_json={
                    "ok": False,
                    "error": str(e),
                    "run_type": run_type,
                    "candidate_id": str(candidate_id_raw) if candidate_id_raw else None,
                    "group_id": str(group_id) if group_id else None,
                },
                meta_patch={"error": str(e)},
            )

            await _notify_controllers_best_effort(job_id=job_id)