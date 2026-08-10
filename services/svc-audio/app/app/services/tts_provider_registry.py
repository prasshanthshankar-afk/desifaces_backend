from __future__ import annotations

from typing import Callable, Dict, Optional

from app.services.tts_provider_adapter import (
    TTSProviderAdapter,
    TTSProviderAdapterError,
)


AdapterFactory = Callable[[], TTSProviderAdapter]


class TTSProviderAdapterRegistry:
    """
    Structural mapping from DB adapter_key to provider adapter.

    Provider selection/ranking remains entirely outside this registry.
    """

    def __init__(
        self,
        factories: Optional[Dict[str, AdapterFactory]] = None,
    ):
        self._factories = (
            dict(factories)
            if factories is not None
            else self._default_factories()
        )

    @staticmethod
    def _default_factories() -> Dict[str, AdapterFactory]:
        def azure():
            from app.services.azure_tts_adapter import AzureTTSAdapter
            return AzureTTSAdapter()

        def elevenlabs():
            from app.services.elevenlabs_tts_adapter import (
                ElevenLabsTTSAdapter,
            )
            return ElevenLabsTTSAdapter()

        def sarvam():
            from app.services.sarvam_tts_adapter import SarvamTTSAdapter
            return SarvamTTSAdapter()

        return {
            "azure": azure,
            "elevenlabs": elevenlabs,
            "sarvam": sarvam,
        }

    def create(self, adapter_key: str) -> TTSProviderAdapter:
        key = str(adapter_key or "").strip().lower()

        if not key:
            raise TTSProviderAdapterError(
                "missing_adapter_key"
            )

        factory = self._factories.get(key)

        if factory is None:
            raise TTSProviderAdapterError(
                f"unknown_adapter_key:{key}"
            )

        return factory()
