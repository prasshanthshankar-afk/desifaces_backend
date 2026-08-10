from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class TTSProviderAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class TTSProviderSynthesisRequest:
    """
    Normalized execution request handed to a provider adapter.

    Provider/model/voice selection already happened before this stage.

    `ssml` is optional because some providers accept SSML while others
    use plain text and provider-native controls.
    """

    provider_code: str
    model_code: str
    provider_model_id: Optional[str]

    voice_name: str

    canonical_locale: str
    provider_locale_code: Optional[str]

    text: str
    output_format: str = "mp3"

    ssml: Optional[str] = None

    style: Optional[str] = None
    emotion: Optional[str] = None

    rate: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0


@dataclass(frozen=True)
class TTSProviderSynthesisResult:
    provider_code: str
    model_code: str
    voice_name: str

    audio_bytes: bytes
    content_type: str
    extension: str

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )


class TTSProviderAdapter(ABC):
    """
    Execution-only provider interface.

    Routing and geographic policy do not belong in adapters.
    """

    @property
    @abstractmethod
    def adapter_key(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def synthesize(
        self,
        request: TTSProviderSynthesisRequest,
    ) -> TTSProviderSynthesisResult:
        raise NotImplementedError
