from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import UUID

from app.repos.music_jobs_repo import MusicJobsRepo
from app.services.audio_probe_service import AudioProbeService
from app.services.clip_manifest_service import ClipManifestService
from app.services.music_orchestrator_common import _as_dict, _as_list, _guess_ext_from_url, _download_http_to_file, _normalize_jsonb_payload
from app.services.music_orchestrator_studio_jobs import persist_studio_payload_best_effort

JsonDict = Dict[str, Any]


def pick_audio_url_for_probe(input_json: JsonDict) -> Optional[str]:
    """
    Prefer BYO/uploaded/master audio URL (available before pipeline),
    else any computed audio URL. Never pick voice_ref_url.
    """
    ij = _normalize_jsonb_payload(input_json)
    hints = _as_dict(ij.get("provider_hints"))
    computed = _normalize_jsonb_payload(ij.get("computed"))

    voice_ref_url = str(computed.get("voice_ref_url") or "").strip()

    candidates = [
        ij.get("uploaded_audio_url"),
        ij.get("audio_master_url"),
        ij.get("audio_url"),
        hints.get("byo_audio_url"),
        hints.get("uploaded_audio_url"),
        hints.get("audio_url"),
        hints.get("audio_master_url"),
        computed.get("audio_master_url"),
        computed.get("byo_audio_url"),
        computed.get("demo_audio_url"),
    ]

    for v in candidates:
        if not v:
            continue
        url = str(v).strip()
        if not url:
            continue
        if voice_ref_url and url == voice_ref_url:
            continue
        return url

    return None


async def maybe_probe_audio_and_update_computed(
    *,
    jobs: MusicJobsRepo,
    job_id: UUID,
    project_id: UUID,
    input_json: JsonDict,
    audio_url: Optional[str],
    duration_ms_hint: Optional[int] = None,
) -> JsonDict:
    computed = _as_dict(input_json.get("computed"))
    ap = _as_dict(computed.get("audio_probe"))
    mp = _as_dict(computed.get("music_plan"))

    dur_ms = None
    try:
        if duration_ms_hint is not None:
            dur_ms = int(float(duration_ms_hint))
    except Exception:
        dur_ms = None

    if not dur_ms:
        for k in ("audio_master_duration_ms", "audio_duration_ms", "uploaded_audio_duration_ms"):
            try:
                v = computed.get(k)
                if v is not None:
                    dur_ms = int(float(v))
                    if dur_ms > 0:
                        break
            except Exception:
                continue

    needs_duration = not isinstance(ap.get("duration_sec"), (int, float)) or float(ap.get("duration_sec") or 0) <= 0
    bpm_val = mp.get("bpm")
    needs_bpm = not isinstance(bpm_val, (int, float)) or float(bpm_val or 0) <= 0

    # Fast-path: known duration => populate deterministic probe
    if needs_duration and dur_ms and dur_ms > 0:
        ap["duration_ms"] = int(dur_ms)
        ap["duration_sec"] = float(dur_ms) / 1000.0
        ap.setdefault("beats_per_bar", 4)
        ap.setdefault("source", "known_duration")
        computed["audio_probe"] = ap

        if not isinstance(computed.get("track_duration_sec"), (int, float)) or float(computed.get("track_duration_sec") or 0) <= 0:
            computed["track_duration_sec"] = float(dur_ms) / 1000.0

        if not isinstance(mp.get("beats_per_bar"), (int, float)) or int(mp.get("beats_per_bar") or 0) <= 0:
            mp["beats_per_bar"] = 4
        computed["music_plan"] = mp

        input_json["computed"] = computed
        await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
        await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)

        # If manifest absent, build it once (safe)
        if not (isinstance(computed.get("clip_manifest"), dict) and _as_list(_as_dict(computed.get("clip_manifest")).get("clips"))):
            try:
                manifest = await ClipManifestService().build_manifest(
                    music_video_job_id=job_id,
                    project_id=project_id,
                    input_json=input_json,
                )
                computed["clip_manifest"] = manifest
                input_json["computed"] = computed
                await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
                await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)
            except Exception:
                pass

        return input_json

    if not audio_url or not needs_duration:
        return input_json

    tmp = Path("/tmp") / f"df_audio_probe_{job_id}{_guess_ext_from_url(audio_url)}"
    probe: Optional[JsonDict] = None

    try:
        await asyncio.to_thread(_download_http_to_file, audio_url, tmp)
        probe = await asyncio.to_thread(AudioProbeService().probe, str(tmp))
    except Exception:
        probe = None
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

    if not probe:
        return input_json

    computed["audio_probe"] = probe

    if not isinstance(computed.get("track_duration_sec"), (int, float)) and isinstance(probe.get("duration_sec"), (int, float)):
        computed["track_duration_sec"] = float(probe["duration_sec"])

    if needs_bpm and isinstance(probe.get("bpm"), (int, float)) and float(probe["bpm"]) > 0:
        mp["bpm"] = float(probe["bpm"])
    if not isinstance(mp.get("beats_per_bar"), (int, float)) or int(mp.get("beats_per_bar") or 0) <= 0:
        mp["beats_per_bar"] = int(probe.get("beats_per_bar") or 4)

    computed["music_plan"] = mp
    input_json["computed"] = computed

    await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
    await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)

    # If manifest absent, build it once (safe)
    if not (isinstance(computed.get("clip_manifest"), dict) and _as_list(_as_dict(computed.get("clip_manifest")).get("clips"))):
        try:
            manifest = await ClipManifestService().build_manifest(
                music_video_job_id=job_id,
                project_id=project_id,
                input_json=input_json,
            )
            computed["clip_manifest"] = manifest
            input_json["computed"] = computed
            await jobs.set_video_job_input_json(job_id=job_id, input_json=input_json)
            await persist_studio_payload_best_effort(job_id=job_id, payload_json=input_json)
        except Exception:
            pass

    return input_json