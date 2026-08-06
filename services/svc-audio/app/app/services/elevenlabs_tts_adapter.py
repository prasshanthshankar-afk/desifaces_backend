from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import quote

from app.services.tts_provider_adapter import (
    TTSProviderAdapter,
    TTSProviderAdapterError,
    TTSProviderSynthesisRequest,
    TTSProviderSynthesisResult,
)


class ElevenLabsTTSAdapter(TTSProviderAdapter):
    """
    ElevenLabs execution adapter.

    Routing/model/voice selection occurs before this adapter.
    """

    def __init__(
        self,
        *,
        client: Optional[Any] = None,
        api_key: Optional[str] = None,
        base_url: str = "https://api.elevenlabs.io",
    ):
        self.client = client
        self.api_key = (
            api_key
            if api_key is not None
            else os.getenv("ELEVENLABS_API_KEY", "")
        ).strip()

        self.base_url = base_url.rstrip("/")

    @property
    def adapter_key(self) -> str:
        return "elevenlabs"

    @staticmethod
    def _output_format(value: str) -> tuple[str, str, str]:
        raw = str(value or "").strip().lower()

        # Start with the documented default-quality MP3 path.
        # Additional formats will be introduced only after explicit
        # provider/tier validation.
        if raw in {"", "mp3"}:
            return (
                "mp3_44100_128",
                "audio/mpeg",
                "mp3",
            )

        raise TTSProviderAdapterError(
            f"unsupported_output_format:{raw}"
        )

    async def synthesize(
        self,
        request: TTSProviderSynthesisRequest,
    ) -> TTSProviderSynthesisResult:
        voice_id = str(
            request.voice_name or ""
        ).strip()

        model_id = str(
            request.provider_model_id or ""
        ).strip()

        text = str(request.text or "").strip()

        if not voice_id:
            raise TTSProviderAdapterError(
                "missing_provider_voice_id"
            )

        if not model_id:
            raise TTSProviderAdapterError(
                "missing_provider_model_id"
            )

        if not text:
            raise TTSProviderAdapterError(
                "missing_synthesis_text"
            )

        if not self.api_key:
            raise TTSProviderAdapterError(
                "missing_provider_credential:elevenlabs"
            )

        provider_format, content_type, extension = (
            self._output_format(
                request.output_format
            )
        )

        client = self.client
        owns_client = False

        if client is None:
            # Runtime-only dependency. Pure unit tests can inject
            # a fake client without requiring httpx on host Python.
            import httpx

            client = httpx.AsyncClient(
                timeout=60.0
            )
            owns_client = True

        url = (
            f"{self.base_url}/v1/text-to-speech/"
            f"{quote(voice_id, safe='')}"
        )

        try:
            response = await client.post(
                url,
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                params={
                    "output_format": provider_format,
                },
                json={
                    "text": text,
                    "model_id": model_id,
                },
            )

            status = int(
                getattr(response, "status_code", 0)
            )

            if status < 200 or status >= 300:
                # Never propagate provider response body:
                # it may contain operational/private detail.
                raise TTSProviderAdapterError(
                    "provider_http_error:"
                    f"elevenlabs:{status}"
                )

            audio = bytes(
                getattr(response, "content", b"")
            )

            if not audio:
                raise TTSProviderAdapterError(
                    "provider_returned_empty_audio"
                )

            return TTSProviderSynthesisResult(
                provider_code=request.provider_code,
                model_code=request.model_code,
                voice_name=request.voice_name,
                audio_bytes=audio,
                content_type=content_type,
                extension=extension,
                metadata={
                    "adapter_key": self.adapter_key,
                    "provider_output_format": (
                        provider_format
                    ),
                    "canonical_locale": (
                        request.canonical_locale
                    ),
                },
            )

        except TTSProviderAdapterError:
            raise

        except Exception as exc:
            raise TTSProviderAdapterError(
                "provider_synthesis_failed:"
                f"elevenlabs:{type(exc).__name__}"
            ) from exc

        finally:
            if owns_client:
                await client.aclose()
