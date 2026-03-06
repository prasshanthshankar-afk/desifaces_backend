from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.music_providers.fal_queue_client import FalQueueClient


@dataclass(frozen=True)
class SonautoAudioFile:
    url: str
    content_type: Optional[str] = None
    file_name: Optional[str] = None
    file_size: Optional[int] = None


@dataclass(frozen=True)
class SonautoResult:
    request_id: str
    seed: int
    tags: List[str]
    lyrics: Optional[str]
    audio_files: List[SonautoAudioFile]

    @property
    def audio(self) -> SonautoAudioFile:
        # Back-compat: old code expected a single audio file.
        return self.audio_files[0]


class FalSonautoV2Provider:
    MODEL_ID = "sonauto/v2/text-to-music"

    def __init__(self):
        self.client = FalQueueClient(
            api_key=getattr(settings, "FAL_KEY", None) or getattr(settings, "FAL_API_KEY", None),
            base_url=getattr(settings, "FAL_QUEUE_BASE_URL", None),
        )

    # -----------------------------
    # Normalizers (robust)
    # -----------------------------
    @staticmethod
    def _safe_int(v: Any, default: int = 0) -> int:
        try:
            return int(float(v))
        except Exception:
            return int(default)

    @staticmethod
    def _safe_float(v: Any, default: float) -> float:
        try:
            return float(v)
        except Exception:
            return float(default)

    @staticmethod
    def _normalize_output_format(v: Any) -> str:
        s = str(v or "").strip().lower() or "wav"
        # Sonauto supports (per docs/behavior): flac, mp3, wav, ogg, m4a
        # Keep permissive but guard against empty/garbage.
        allowed = {"flac", "mp3", "wav", "ogg", "m4a"}
        return s if s in allowed else "wav"

    @staticmethod
    def _normalize_bpm(v: Any) -> Any:
        """
        Sonauto accepts bpm numeric or "auto".
        """
        if v is None:
            return None
        if isinstance(v, (int, float)):
            n = int(v)
            return max(40, min(220, n))
        s = str(v).strip().lower()
        if not s:
            return "auto"
        if s == "auto":
            return "auto"
        try:
            n = int(float(s))
            return max(40, min(220, n))
        except Exception:
            # keep custom strings if provider supports them; else "auto"
            return s

    @staticmethod
    def _normalize_bit_rate(v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            n = int(float(v))
        except Exception:
            return None
        return n if n in (128, 192, 256, 320) else None

    @staticmethod
    def _clean_tags(tags: Any) -> List[str]:
        """
        Accept:
          - list[str]
          - "a,b,c"
          - JSON list string: '["a","b"]'
        Returns: deduped list[str], capped.
        """
        if tags is None:
            return []

        raw: List[Any] = []
        if isinstance(tags, list):
            raw = tags
        elif isinstance(tags, str):
            s = tags.strip()
            if not s:
                raw = []
            elif s.startswith("["):
                try:
                    obj = __import__("json").loads(s)
                    raw = obj if isinstance(obj, list) else [s]
                except Exception:
                    raw = [p.strip() for p in s.split(",")]
            else:
                raw = [p.strip() for p in s.split(",")]
        else:
            raw = [tags]

        # sanitize + dedupe
        out: List[str] = []
        seen = set()
        max_tags = int(getattr(settings, "MUSIC_SONAUTO_MAX_TAGS", 40) or 40)
        max_len = int(getattr(settings, "MUSIC_SONAUTO_MAX_TAG_LEN", 64) or 64)

        for x in raw:
            t = str(x or "").strip()
            if not t:
                continue
            if len(t) > max_len:
                t = t[:max_len]
            k = t.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(t)
            if len(out) >= max_tags:
                break

        return out

    @staticmethod
    def _choose_inputs(
        *,
        prompt: Optional[str],
        tags: List[str],
        lyrics_prompt: Optional[str],
        lyrics_is_user: bool,
        instrumental: bool,
    ) -> Dict[str, Any]:
        """
        Sonauto (queue.fal.run) requirements:
          - Provide at least one of: prompt, tags, lyrics_prompt
          - Instrumental is achieved by lyrics_prompt="" (empty string)
          - prompt+tags is allowed

        We ensure we never send an empty payload (which would trigger:
          "one of tags, lyrics, or prompt must be provided").
        """
        p = (prompt or "").strip() or None
        t = tags or []

        # For instrumental, explicitly set empty string lyrics_prompt
        if instrumental:
            lp: Optional[str] = ""
        else:
            lp = None if lyrics_prompt is None else str(lyrics_prompt)

        out: Dict[str, Any] = {}

        # If lyrics_prompt is provided (including instrumental "")
        if lp is not None:
            if t:
                out["tags"] = t
            if p:
                out["prompt"] = p
            out["lyrics_prompt"] = lp

            # Guard: if instrumental and both prompt/tags missing, include minimal prompt
            if instrumental and ("prompt" not in out) and ("tags" not in out):
                out["prompt"] = "An instrumental track."

            # Guard: if user explicitly gave lyrics but provided nothing else, add minimal prompt
            if lyrics_is_user and (not t) and (p is None) and (not instrumental):
                out["prompt"] = "Create a song with these lyrics."
            return out

        # No lyrics_prompt
        if t:
            out["tags"] = t
        if p:
            out["prompt"] = p

        if not out:
            out["prompt"] = "Create an original song."
        return out

    @staticmethod
    def _parse_audio_files(resp: Any) -> List[SonautoAudioFile]:
        if not isinstance(resp, dict):
            return []

        audio_field = resp.get("audio")
        items: List[dict] = []
        if isinstance(audio_field, list):
            items = [x for x in audio_field if isinstance(x, dict)]
        elif isinstance(audio_field, dict):
            items = [audio_field]

        out: List[SonautoAudioFile] = []
        for a in items:
            url = str(a.get("url") or "").strip()
            if not url:
                continue

            ct = a.get("content_type")
            fn = a.get("file_name")
            fs = a.get("file_size")

            file_size: Optional[int] = None
            if isinstance(fs, int):
                file_size = fs
            elif isinstance(fs, str):
                try:
                    file_size = int(float(fs))
                except Exception:
                    file_size = None

            out.append(
                SonautoAudioFile(
                    url=url,
                    content_type=str(ct).strip() if isinstance(ct, str) and ct.strip() else None,
                    file_name=str(fn).strip() if isinstance(fn, str) and fn.strip() else None,
                    file_size=file_size,
                )
            )
        return out

    # -----------------------------
    # Main API
    # -----------------------------
    async def generate(
        self,
        *,
        prompt: Optional[str],
        tags: Any = None,
        lyrics_prompt: Optional[str] = None,
        lyrics_is_user_provided: bool = False,
        instrumental: bool = False,
        seed: Optional[int] = None,
        output_format: str = "wav",
        output_bit_rate: Optional[int] = 192,
        bpm: Any = "auto",
        prompt_strength: float = 2.0,
        balance_strength: float = 0.7,
        num_songs: int = 1,
    ) -> SonautoResult:
        t = self._clean_tags(tags)

        payload = self._choose_inputs(
            prompt=prompt,
            tags=t,
            lyrics_prompt=lyrics_prompt,
            lyrics_is_user=bool(lyrics_is_user_provided),
            instrumental=bool(instrumental),
        )

        # Clamp tunables to sane ranges (avoid provider errors / weirdness)
        ps = self._safe_float(prompt_strength, 2.0)
        bs = self._safe_float(balance_strength, 0.7)
        payload["prompt_strength"] = max(0.1, min(5.0, ps))
        payload["balance_strength"] = max(0.0, min(1.0, bs))

        ns = self._safe_int(num_songs, 1)
        ns = max(1, min(int(getattr(settings, "MUSIC_SONAUTO_MAX_NUM_SONGS", 4) or 4), ns))
        payload["num_songs"] = ns

        of = self._normalize_output_format(output_format)
        payload["output_format"] = of

        br = self._normalize_bit_rate(output_bit_rate)
        if of in ("mp3", "m4a") and br is not None:
            payload["output_bit_rate"] = br

        bpm_val = self._normalize_bpm(bpm)
        if bpm_val is not None:
            payload["bpm"] = bpm_val

        if seed is not None:
            payload["seed"] = int(seed)

        # IMPORTANT: queue.fal.run expects TOP-LEVEL fields (NOT {"input": {...}})
        submit = await self.client.submit(
            model_id=self.MODEL_ID,
            payload=payload,
            object_lifecycle_seconds=int(getattr(settings, "MUSIC_FAL_OBJECT_LIFECYCLE_SECONDS", 3600) or 3600),
            start_timeout_seconds=getattr(settings, "MUSIC_FAL_START_TIMEOUT_SECONDS", None),
        )

        resp = await self.client.wait_for_completion(
            status_url=submit.status_url,
            response_url=submit.response_url,
            poll_seconds=float(getattr(settings, "MUSIC_FAL_POLL_SECONDS", 2.5) or 2.5),
            timeout_seconds=int(getattr(settings, "MUSIC_FAL_TIMEOUT_SECONDS", 900) or 900),
        )

        if not isinstance(resp, dict):
            raise RuntimeError("fal_sonauto_invalid_response")

        seed_out = self._safe_int(resp.get("seed"), self._safe_int(seed, 0))
        tags_out = self._clean_tags(resp.get("tags"))
        lyrics_out = resp.get("lyrics")
        lyrics_out = str(lyrics_out) if isinstance(lyrics_out, str) else None

        audio_files = self._parse_audio_files(resp)
        if not audio_files:
            raise RuntimeError("fal_sonauto_missing_audio_url")

        return SonautoResult(
            request_id=submit.request_id,
            seed=seed_out,
            tags=tags_out,
            lyrics=lyrics_out,
            audio_files=audio_files,
        )