from __future__ import annotations

from typing import Any, Optional

from app.services.tts_provider_adapter import (
    TTSProviderAdapter,
    TTSProviderAdapterError,
    TTSProviderSynthesisRequest,
    TTSProviderSynthesisResult,
)


class AzureTTSAdapter(TTSProviderAdapter):
    """
    Compatibility adapter around the existing AzureTTSService.

    This is execution logic only.

    Provider selection is performed before this adapter is invoked.
    """

    def __init__(
        self,
        service: Optional[Any] = None,
    ):
        # Tests may inject a fake service so unit validation never
        # requires credentials or external network calls.
        if service is not None:
            self.service = service
        else:
            # Runtime-only dependency.
            #
            # Keep the concrete Azure implementation out of module
            # import time so pure adapter unit tests do not require
            # the full svc-audio runtime dependency set.
            from app.services.azure_tts_service import (
                AzureTTSService,
            )

            self.service = AzureTTSService()

    @property
    def adapter_key(self) -> str:
        return "azure"

    @staticmethod
    def _normalize_output_format(
        value: str,
    ) -> str:
        raw = str(value or "").strip().lower()

        if raw in {"", "mp3"}:
            return "mp3"

        if raw in {"wav", "wave"}:
            return "wav"

        raise TTSProviderAdapterError(
            f"unsupported_output_format:{raw}"
        )

    async def synthesize(
        self,
        request: TTSProviderSynthesisRequest,
    ) -> TTSProviderSynthesisResult:
        ssml = str(
            request.ssml or ""
        ).strip()

        if not ssml:
            raise TTSProviderAdapterError(
                "azure_adapter_requires_ssml"
            )

        output_format = (
            self._normalize_output_format(
                request.output_format
            )
        )

        try:
            if output_format == "wav":
                audio_bytes = (
                    await self.service.synthesize_wav(
                        ssml=ssml
                    )
                )
                content_type = "audio/wav"
                extension = "wav"

            else:
                audio_bytes = (
                    await self.service.synthesize_mp3(
                        ssml=ssml
                    )
                )
                content_type = "audio/mpeg"
                extension = "mp3"

        except TTSProviderAdapterError:
            raise

        except Exception as exc:
            # Do not include provider response bodies, credentials,
            # request headers, or arbitrary exception strings here.
            raise TTSProviderAdapterError(
                "provider_synthesis_failed:"
                f"{self.adapter_key}:"
                f"{type(exc).__name__}"
            ) from exc

        if not isinstance(
            audio_bytes,
            (bytes, bytearray),
        ):
            raise TTSProviderAdapterError(
                "provider_returned_non_bytes_audio"
            )

        audio = bytes(audio_bytes)

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
                "canonical_locale": (
                    request.canonical_locale
                ),
                "provider_locale_code": (
                    request.provider_locale_code
                ),
            },
        )
