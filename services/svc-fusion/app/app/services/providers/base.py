from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ProviderEstimate:
    variant_code: Optional[str] = None
    leaf_sku_code: Optional[str] = None
    estimated_units: str = "1"
    unit_type: str = "minute"
    provider_meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderPrepareInput:
    job_id: str
    user_id: str
    request_payload: Dict[str, Any]
    resolved_face_url: Optional[str] = None
    resolved_audio_url: Optional[str] = None
    reference_image_urls: Optional[List[str]] = None


@dataclass
class ProviderPrepareResult:
    provider_name: str
    provider_version: str
    request_json: Dict[str, Any]
    submit_meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderSubmitResult:
    provider_job_id: str
    raw_response: Dict[str, Any]


@dataclass
class ProviderPollResult:
    status: str  # processing | succeeded | failed | canceled | unknown
    video_url: Optional[str] = None
    share_url: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    error_code: Optional[str] = None


class ProviderClient(Protocol):
    provider_name: str
    provider_version: str

    async def estimate(self, request_payload: Dict[str, Any]) -> ProviderEstimate:
        ...

    async def prepare(self, spec: ProviderPrepareInput) -> ProviderPrepareResult:
        ...

    async def submit(self, payload: Dict[str, Any], idempotency_key: str) -> ProviderSubmitResult:
        ...

    async def poll(self, provider_job_id: str) -> ProviderPollResult:
        ...

    async def get_share_url(self, provider_job_id: str) -> Optional[str]:
        ...
