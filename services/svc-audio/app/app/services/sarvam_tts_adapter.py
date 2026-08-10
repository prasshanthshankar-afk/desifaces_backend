from __future__ import annotations

import base64
import os
from typing import Any, Optional

from app.services.tts_provider_adapter import (
    TTSProviderAdapter,
    TTSProviderAdapterError,
    TTSProviderSynthesisRequest,
    TTSProviderSynthesisResult,
)


class SarvamTTSAdapter(TTSProviderAdapter):
    """
    Sarvam execution adapter.

    Provider/model/locale/voice selection occurs before this adapter.
    """

    def __init__(
        self,
        *,
        client: Optional[Any] = None,
        api_key: Optional[str] = None,
        base_url: str = "https://api.sarvam.ai",
    ):
        self.client = client

        self.api_key = (
            api_key
            if api_key is not None
            else os.getenv("SARVAM_API_KEY", "")
        ).strip()

        self.base_url = base_url.rstrip("/")

    @property
    def adapter_key(self) -> str:
        return "sarvam"

    @staticmethod
    def _output_format(
        value: str,
    ) -> tuple[str, str, str]:
        raw = str(value or "").strip().lower()

        if raw in {"", "mp3"}:
            return (
                "mp3",
                "audio/mpeg",
                "mp3",
            )

        if raw in {"wav", "wave"}:
            return (
                "wav",
                "audio/wav",
                "wav",
            )

        raise TTSProviderAdapterError(
            f"unsupported_output_format:{raw}"
        )

    async def synthesize(
        self,
        request: TTSProviderSynthesisRequest,
    ) -> TTSProviderSynthesisResult:
        text = str(request.text or "").strip()

        speaker = str(
            request.voice_name or ""
        ).strip()

        model = str(
            request.provider_model_id or ""
        ).strip()

        provider_locale = str(
            request.provider_locale_code or ""
        ).strip()

        if not text:
            raise TTSProviderAdapterError(
                "missing_synthesis_text"
            )

        if not speaker:
            raise TTSProviderAdapterError(
                "missing_provider_voice_id"
            )

        if not model:
            raise TTSProviderAdapterError(
                "missing_provider_model_id"
            )

        if not provider_locale:
            raise TTSProviderAdapterError(
                "missing_provider_locale_code"
            )

        if not self.api_key:
            raise TTSProviderAdapterError(
                "missing_provider_credential:sarvam"
            )

        codec, content_type, extension = (
            self._output_format(
                request.output_format
            )
        )

        payload = {
            "text": text,
            "target_language_code": provider_locale,
            "speaker": speaker,
            "model": model,
            "output_audio_codec": codec,
        }

        # Provider-neutral rate maps to Sarvam's pace.
        # v3 supports 0.5–2.0.
        pace = float(request.rate)

        if pace < 0.5 or pace > 2.0:
            raise TTSProviderAdapterError(
                f"unsupported_pace:{pace}"
            )

        payload["pace"] = pace

        client = self.client
        owns_client = False

        if client is None:
            # Runtime-only dependency.
            import httpx

            client = httpx.AsyncClient(
                timeout=60.0
            )
            owns_client = True


        try:
            response = await client.post(
                f"{self.base_url}/text-to-speech",
                headers={
                    "api-subscription-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            status = int(
                getattr(response, "status_code", 0)
            )

            if status < 200 or status >= 300:
                raise TTSProviderAdapterError(
                    "provider_http_error:"
                    f"sarvam:{status}"
                )

            try:
                body = response.json()
            except Exception as exc:
                raise TTSProviderAdapterError(
                    "provider_invalid_json:sarvam"
                ) from exc

            audios = (
                body.get("audios")
                if isinstance(body, dict)
                else None
            )

            if (
                not isinstance(audios, list)
                or not audios
                or not isinstance(audios[0], str)
                or not audios[0].strip()
            ):
                raise TTSProviderAdapterError(
                    "provider_missing_audio:sarvam"
                )

            try:
                audio = base64.b64decode(
                    audios[0],
                    validate=True,
                )
            except Exception as exc:
                raise TTSProviderAdapterError(
                    "provider_invalid_audio_encoding:sarvam"
                ) from exc

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
                    "provider_locale_code": provider_locale,
                    "provider_output_codec": codec,
                    "request_id": (
                        body.get("request_id")
                        if isinstance(body, dict)
                        else None
                    ),
                },
            )

        except TTSProviderAdapterError:
            raise

        except Exception as exc:
            raise TTSProviderAdapterError(
                "provider_synthesis_failed:"
                f"sarvam:{type(exc).__name__}"
            ) from exc

        finally:
            if owns_client:
                await client.aclose()
