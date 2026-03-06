from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.services.azure_storage_service import AzureStorageService


@dataclass(frozen=True)
class AutopilotComposeResult:
    sas_url: str
    duration_ms: int
    content_type: str
    provider: str
    provider_request_id: str
    provider_seed: int
    source_url: str
    tags: list[str]
    lyrics: Optional[str]


def normalize_provider(p: Any) -> str:
    return str(p or "").strip().lower().replace("-", "_")


def default_autopilot_provider() -> str:
    # Prefer env var keys too (settings may not expose them)
    fal_key = (getattr(settings, "FAL_KEY", None) or "").strip() or (os.getenv("FAL_KEY") or "").strip() or (os.getenv("FAL_API_KEY") or "").strip()
    return "fal_sonauto_v2" if fal_key else "native"


def _guess_audio_content_type_from_ext(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    if e == "wav":
        return "audio/wav"
    if e == "mp3":
        return "audio/mpeg"
    if e == "m4a":
        return "audio/mp4"
    if e == "ogg" or e == "opus":
        return "audio/ogg"
    return "audio/mpeg"


def _wav_duration_ms(path: str) -> int:
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 0
            if rate <= 0:
                return 0
            sec = float(frames) / float(rate)
            return max(0, int(sec * 1000.0))
    except Exception:
        return 0


def _ffprobe_duration_ms(path: str) -> int:
    """
    Prefer ffprobe if available (works for mp3/wav/m4a/ogg). If ffprobe is missing,
    fall back to wave-duration for WAV; otherwise return 0.
    """
    if not shutil.which("ffprobe"):
        if str(path).lower().endswith(".wav"):
            return _wav_duration_ms(path)
        return 0

    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nk=1:nw=1",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        out = (p.stdout or "").strip()
        if not out:
            return 0
        sec = float(out)
        return max(0, int(sec * 1000.0))
    except Exception:
        if str(path).lower().endswith(".wav"):
            return _wav_duration_ms(path)
        return 0


async def _download_to_file(url: str, *, suffix: str) -> str:
    """
    Downloads URL to a temp file and returns local path.
    Uses httpx if installed; otherwise uses urllib in a thread via anyio.
    """
    u = (url or "").strip()
    if not u:
        raise ValueError("download_url_required")

    fd, path = tempfile.mkstemp(prefix="df_sonauto_", suffix=suffix)
    os.close(fd)

    # First try httpx (streaming + redirects)
    try:
        import httpx  # type: ignore

        try:
            async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
                async with client.stream("GET", u) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        async for chunk in r.aiter_bytes():
                            if chunk:
                                f.write(chunk)
            return path
        except Exception:
            try:
                os.unlink(path)
            except Exception:
                pass
            raise
    except Exception:
        # No httpx -> urllib fallback
        try:
            import anyio
            import urllib.request

            def _sync_download() -> None:
                req = urllib.request.Request(u, headers={"User-Agent": "desifaces-svc-music"})
                with urllib.request.urlopen(req, timeout=180) as resp:  # nosec - controlled URL
                    with open(path, "wb") as f:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)

            try:
                await anyio.to_thread.run_sync(_sync_download)
                return path
            except Exception:
                try:
                    os.unlink(path)
                except Exception:
                    pass
                raise
        except Exception:
            try:
                os.unlink(path)
            except Exception:
                pass
            raise RuntimeError("missing_dependency_httpx_or_anyio")


def _as_dict(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
    return {}


def _as_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, list) else []
            except Exception:
                return []
        # allow comma separated
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]
        return [s]
    return []


def _first_nonempty_str(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def _clamp_float(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except Exception:
        return default
    return max(lo, min(hi, x))


def _normalize_tags(hints: Dict[str, Any], computed: Dict[str, Any]) -> List[str]:
    """
    Sonauto tags should ideally come from Sonauto tag explorer.
    We keep this conservative to avoid 422 from invalid tag combinations.
    """
    raw = hints.get("sonauto_tags") or hints.get("tags") or []
    tags: List[str] = []
    for t in _as_list(raw):
        ts = str(t or "").strip()
        if ts:
            tags.append(ts)

    # Dedup (preserve order)
    out: List[str] = []
    seen = set()
    for t in tags:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out[:30]


def _build_prompt(*, language_hint: str | None, hints: Dict[str, Any], computed: Dict[str, Any]) -> str:
    """
    Priority:
      1) computed.audio_style_prompt (from music_tools.py)
      2) hints.audio_prompt / hints.music_prompt / hints.prompt
      3) computed.plan_summary
      4) fallback (title/genre/mood/tempo)
    """
    lang = language_hint or "en"

    p = _first_nonempty_str(
        computed.get("audio_style_prompt"),
        hints.get("audio_prompt"),
        hints.get("music_prompt"),
        hints.get("prompt"),
    )
    if p:
        if "language:" not in p.lower():
            return f"{p} Language: {lang}."
        return p

    ps = str(computed.get("plan_summary") or "").strip()
    if ps:
        return f"{ps}. Language: {lang}."

    title = str(hints.get("title") or "Untitled").strip()
    genre = str(hints.get("genre") or hints.get("genre_hint") or "pop").strip()
    mood = str(hints.get("mood") or hints.get("vibe_hint") or "uplifting").strip()
    tempo = str(hints.get("tempo") or "mid").strip()

    return f"A {genre} song with a {mood} vibe, tempo {tempo}, titled '{title}'. Language: {lang}."


def _pick_bpm(hints: Dict[str, Any], computed: Dict[str, Any]) -> Any:
    """
    Sonauto accepts bpm="auto" or integer.
    """
    v = hints.get("bpm")
    if v is not None:
        try:
            return int(float(v))
        except Exception:
            return v
    v2 = hints.get("tempo_bpm")
    if v2 is not None:
        try:
            return int(float(v2))
        except Exception:
            return v2
    v3 = computed.get("audio_bpm")
    if v3 is not None:
        try:
            return int(float(v3))
        except Exception:
            return v3
    return "auto"


def _extract_sonauto_audio(result_json: Dict[str, Any]) -> Tuple[str, str]:
    """
    Result has `audio` (dict or list of dicts) with url + content_type.
    """
    audio = result_json.get("audio")

    if isinstance(audio, dict):
        url = str(audio.get("url") or "").strip()
        if url:
            ct = str(audio.get("content_type") or "").strip() or "audio/wav"
            return url, ct

    if isinstance(audio, list):
        for it in audio:
            if isinstance(it, dict):
                url = str(it.get("url") or "").strip()
                if url:
                    ct = str(it.get("content_type") or "").strip() or "audio/wav"
                    return url, ct

    # sometimes nested under response
    resp = result_json.get("response")
    if isinstance(resp, dict):
        return _extract_sonauto_audio(resp)

    raise RuntimeError(f"sonauto_missing_audio_in_response:{str(result_json)[:800]}")


async def _upload_audio_and_get_sas_url(
    *,
    user_id: str,
    project_id: str,
    job_id: str,
    local_path: str,
    content_type: str,
    blob_filename: str,
) -> str:
    """
    Azure upload wrapper: works across differing AzureStorageService method names.
    """
    storage = AzureStorageService.for_output() if hasattr(AzureStorageService, "for_output") else AzureStorageService()  # type: ignore

    candidates = [
        "upload_music_audio_and_get_sas_url",
        "upload_audio_and_get_sas_url",
        "upload_file_and_get_sas_url",
        "upload_local_file_and_get_sas_url",
        "upload_and_get_sas_url",
        # last resort (exists in some branches)
        "upload_music_fallback_audio_and_get_sas_url",
    ]

    kwargs = {
        "user_id": user_id,
        "project_id": project_id,
        "job_id": job_id,
        "local_path": local_path,
        "path": local_path,
        "file_path": local_path,
        "content_type": content_type,
        "mime": content_type,
        "blob_filename": blob_filename,
        "filename": blob_filename,
        "dst_filename": blob_filename,
    }

    for name in candidates:
        fn = getattr(storage, name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
            call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
            res = fn(**call_kwargs)
            if inspect.isawaitable(res):
                res = await res
            out = str(res or "").strip()
            if out:
                return out
        except Exception:
            continue

    raise RuntimeError("azure_storage_upload_method_missing_for_music_audio")


async def _fal_queue_submit_and_wait(
    *,
    model_id: str,
    fal_key: str,
    payload: Dict[str, Any],
    timeout_submit_s: float,
    timeout_poll_s: float,
    poll_s: float,
) -> Tuple[str, Dict[str, Any]]:
    """
    Submit to fal queue and wait for COMPLETED; return (request_id, result_json).
    """
    try:
        import httpx  # type: ignore
    except Exception as e:
        raise RuntimeError("missing_httpx_required_for_fal_queue") from e

    base = f"https://queue.fal.run/{model_id.strip().strip('/')}"
    headers = {
        "Authorization": f"Key {fal_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # keep artifacts around for 1 day
        "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": 86400}),
    }

    async with httpx.AsyncClient(timeout=timeout_submit_s, follow_redirects=True) as client:
        r = await client.post(base, headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"fal_submit_failed:{r.status_code}:{(r.text or '')[:1200]} payload_keys={sorted(list(payload.keys()))}")

        j = r.json() if isinstance(r.json(), dict) else {}
        request_id = str(j.get("request_id") or j.get("id") or "").strip()
        status_url = str(j.get("status_url") or "").strip() or f"{base}/requests/{request_id}/status"
        response_url = str(j.get("response_url") or "").strip() or f"{base}/requests/{request_id}"

    if not request_id:
        raise RuntimeError("fal_submit_missing_request_id")

    t0 = time.time()
    last_status: Dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        while True:
            if time.time() - t0 > timeout_poll_s:
                raise RuntimeError(f"fal_timeout:{model_id}:{request_id}")

            rs = await client.get(f"{status_url}?logs=1", headers={"Authorization": f"Key {fal_key}", "Accept": "application/json"})
            if rs.status_code >= 400:
                raise RuntimeError(f"fal_status_failed:{rs.status_code}:{(rs.text or '')[:1200]}")
            last_status = rs.json() if isinstance(rs.json(), dict) else {}
            st = str(last_status.get("status") or "").strip().upper()

            if st == "COMPLETED":
                break
            if st in ("FAILED", "CANCELED", "CANCELLED"):
                raise RuntimeError(f"fal_failed:{model_id}:{request_id}:{str(last_status)[:1200]}")

            await asyncio.sleep(poll_s)  # type: ignore[name-defined]

        rr = await client.get(response_url, headers={"Authorization": f"Key {fal_key}", "Accept": "application/json"})
        if rr.status_code >= 400:
            raise RuntimeError(f"fal_result_failed:{rr.status_code}:{(rr.text or '')[:1600]}")
        result = rr.json() if isinstance(rr.json(), dict) else {}

    return request_id, result


async def compose_full_mix_fal_sonauto_v2(
    *,
    user_id: str,
    project_id: str,
    job_id: str,
    language_hint: str | None,
    quality: str,
    seed: int | None,
    hints: Dict[str, Any],
    computed: Dict[str, Any],
) -> AutopilotComposeResult:
    """
    Correct Sonauto v2 usage (Fal):
      - Use model id: sonauto/v2/text-to-music
      - Provide at least one of prompt/tags/lyrics_prompt
      - Lyrics must be paired with prompt OR tags
      - Do NOT provide all three simultaneously (prompt+tags+lyrics_prompt)
    """
    fal_key = (getattr(settings, "FAL_KEY", None) or "").strip() or (os.getenv("FAL_KEY") or "").strip() or (os.getenv("FAL_API_KEY") or "").strip()
    if not fal_key:
        raise RuntimeError("missing_fal_key:set_FAL_KEY_or_FAL_API_KEY")

    q = str(quality or "standard").strip().lower()
    # svc-music quality is draft|standard|pro; provider quality mapping is local
    if q == "pro":
        ext = "wav"
        output_format = "wav"
        bit_rate = None
    elif q == "draft":
        ext = "mp3"
        output_format = "mp3"
        bit_rate = 128
    else:
        ext = "mp3"
        output_format = "mp3"
        bit_rate = 192

    prompt = _build_prompt(language_hint=language_hint, hints=hints, computed=computed)

    # Use lyrics if present (generated or uploaded). Truncate to avoid provider rejection.
    lyrics_text: Optional[str] = None
    lt = computed.get("lyrics_text")
    if isinstance(lt, str) and lt.strip():
        # If explicitly none, don't send
        src_eff = str(computed.get("lyrics_source_effective") or "").strip().lower()
        if src_eff != "none":
            lyrics_text = lt.strip()[:4000]

    # Tags: OFF by default for reliability; turn on only if explicitly requested and no lyrics (avoids invalid combo)
    use_tags = str(hints.get("sonauto_use_tags") or os.getenv("DF_SONAUTO_USE_TAGS") or "").strip().lower() in ("1", "true", "yes", "y", "on")
    tags = _normalize_tags(hints=hints, computed=computed) if use_tags else []

    # Strength knobs: clamp to reasonable ranges to reduce 422 risk
    prompt_strength = _clamp_float(hints.get("prompt_strength"), 2.0, 1.4, 3.1)
    balance_strength = _clamp_float(hints.get("balance_strength"), 0.7, 0.1, 1.0)

    bpm = _pick_bpm(hints=hints, computed=computed)
    num_songs = max(1, min(2, _coerce_int(hints.get("num_songs") or 1, 1)))  # keep 1-2 for stability

    # ---- Build Sonauto payload with schema-correct combinations ----
    payload: Dict[str, Any] = {
        "output_format": output_format,
        "num_songs": num_songs,
        "prompt_strength": prompt_strength,
        "balance_strength": balance_strength,
        "bpm": bpm,
    }
    if seed is not None:
        payload["seed"] = int(seed)

    # If we have lyrics: send prompt + lyrics_prompt, and DO NOT send tags (avoid prompt+tags+lyrics)
    if lyrics_text:
        payload["prompt"] = prompt[:900].strip() or "A modern song."
        payload["lyrics_prompt"] = lyrics_text
    else:
        # No lyrics: prompt-only by default; optionally prompt+tags if explicitly enabled
        payload["prompt"] = prompt[:900].strip() or "A modern song."
        if tags:
            payload["tags"] = tags

    if bit_rate is not None and output_format in ("mp3", "m4a"):
        payload["output_bit_rate"] = int(max(128, min(320, int(bit_rate))))

    # Correct model id (the previous 422 URL you saw was missing /text-to-music)
    model_id = str(os.getenv("DF_FAL_SONAUTO_MODEL_ID") or "sonauto/v2/text-to-music").strip()
    timeout_submit_s = float(os.getenv("DF_FAL_SUBMIT_TIMEOUT_SECS") or 60)
    timeout_poll_s = float(os.getenv("DF_FAL_POLL_TIMEOUT_SECS") or 900)
    poll_s = float(os.getenv("DF_FAL_POLL_SECS") or 2)

    # Poll via fal queue
    try:
        import asyncio  # noqa: F401
    except Exception:
        # should never happen in python, but keep safe
        raise RuntimeError("asyncio_missing")

    request_id, result = await _fal_queue_submit_and_wait(
        model_id=model_id,
        fal_key=fal_key,
        payload=payload,
        timeout_submit_s=timeout_submit_s,
        timeout_poll_s=timeout_poll_s,
        poll_s=poll_s,
    )

    audio_url, provider_ct = _extract_sonauto_audio(result)
    provider_ct = provider_ct or _guess_audio_content_type_from_ext(ext)

    local_path = await _download_to_file(audio_url, suffix=f".{ext}")
    try:
        duration_ms = _ffprobe_duration_ms(local_path) or 90_000  # Sonauto v2 often ~1.5m
        content_type = provider_ct or _guess_audio_content_type_from_ext(ext)

        blob_filename = f"full_mix_{job_id}.{ext}"
        sas_url = await _upload_audio_and_get_sas_url(
            user_id=user_id,
            project_id=project_id,
            job_id=job_id,
            local_path=local_path,
            content_type=content_type,
            blob_filename=blob_filename,
        )

        return AutopilotComposeResult(
            sas_url=str(sas_url),
            duration_ms=int(duration_ms),
            content_type=str(content_type),
            provider="fal_sonauto_v2",
            provider_request_id=str(request_id),
            provider_seed=int(seed or 0),
            source_url=str(audio_url),
            tags=list(tags or []),
            lyrics=lyrics_text,
        )
    finally:
        try:
            os.unlink(local_path)
        except Exception:
            pass