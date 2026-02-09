from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import settings
from app.services.azure_storage_service import AzureStorageService
from app.services.music_providers.fal_sonauto_v2_provider import FalSonautoV2Provider


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
    if (getattr(settings, "FAL_KEY", None) or "").strip():
        return "fal_sonauto_v2"
    return "native"


def _guess_audio_content_type_from_ext(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    if e == "wav":
        return "audio/wav"
    if e == "mp3":
        return "audio/mpeg"
    if e == "m4a":
        return "audio/mp4"
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
    Prefer ffprobe if available (works for mp3/wav/m4a). If ffprobe is missing,
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

    # First try httpx (nice streaming + redirects)
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


def _build_prompt(*, language_hint: str | None, hints: Dict[str, Any], computed: Dict[str, Any]) -> str:
    ps = str(computed.get("plan_summary") or "").strip()
    if ps:
        return f"{ps}. Language: {language_hint or 'en'}."

    title = str(hints.get("title") or "Untitled").strip()
    genre = str(hints.get("genre") or hints.get("genre_hint") or "pop").strip()
    mood = str(hints.get("mood") or hints.get("vibe_hint") or "uplifting").strip()
    tempo = str(hints.get("tempo") or "mid").strip()
    lang = language_hint or "en"

    return f"A {genre} song with a {mood} vibe, tempo {tempo}, titled '{title}'. Language: {lang}."


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
    if not (getattr(settings, "FAL_KEY", None) or "").strip():
        raise RuntimeError("missing_fal_key")

    q = str(quality or "standard").strip().lower()
    if q in ("high", "hq", "premium"):
        ext = "wav"
        output_format = "wav"
        bit_rate = None
    elif q in ("draft", "low"):
        ext = "mp3"
        output_format = "mp3"
        bit_rate = 128
    else:
        ext = "mp3"
        output_format = "mp3"
        bit_rate = 192

    prompt = _build_prompt(language_hint=language_hint, hints=hints, computed=computed)

    # only pass lyrics when the user explicitly uploaded them
    lyrics_text: Optional[str] = None
    if str(computed.get("lyrics_source_effective") or "").strip().lower() == "upload":
        lt = computed.get("lyrics_text")
        lyrics_text = str(lt).strip() if isinstance(lt, str) and lt.strip() else None

    tags = hints.get("tags") or hints.get("style_tags") or hints.get("style_refs")

    # optional tunables
    prompt_strength = hints.get("prompt_strength", 2.0)
    balance_strength = hints.get("balance_strength", 0.7)

    provider = FalSonautoV2Provider()
    res = await provider.generate(
        prompt=prompt,
        tags=tags,
        lyrics_prompt=lyrics_text,
        lyrics_is_user_provided=bool(lyrics_text),
        instrumental=bool(hints.get("instrumental")),
        seed=seed,
        output_format=output_format,
        output_bit_rate=bit_rate,
        bpm=hints.get("bpm", "auto"),
        prompt_strength=float(prompt_strength or 2.0),
        balance_strength=float(balance_strength or 0.7),
        num_songs=int(hints.get("num_songs") or 1),
    )

    local_path = await _download_to_file(res.audio.url, suffix=f".{ext}")

    try:
        duration_ms = _ffprobe_duration_ms(local_path) or 30_000
        content_type = res.audio.content_type or _guess_audio_content_type_from_ext(ext)

        storage = AzureStorageService.for_output()
        sas_url = await storage.upload_music_fallback_audio_and_get_sas_url(
            user_id=user_id,
            project_id=project_id,
            job_id=job_id,
            local_path=local_path,
            content_type=content_type,
            blob_filename=f"full_mix.{ext}",
        )

        return AutopilotComposeResult(
            sas_url=str(sas_url),
            duration_ms=int(duration_ms),
            content_type=str(content_type),
            provider="fal_sonauto_v2",
            provider_request_id=str(res.request_id),
            provider_seed=int(res.seed),
            source_url=str(res.audio.url),
            tags=list(res.tags or []),
            lyrics=res.lyrics,
        )
    finally:
        try:
            os.unlink(local_path)
        except Exception:
            pass