from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol


@dataclass
class ProviderSubmitResult:
    provider_job_id: str
    raw_response: Dict[str, Any]


@dataclass
class ProviderPollResult:
    status: str  # "processing" | "succeeded" | "failed"
    video_url: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class ProviderClient(Protocol):
    provider_name: str

    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        ...

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        ...