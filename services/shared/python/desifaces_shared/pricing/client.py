# services/shared/python/desifaces_shared/pricing/client.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Type, TypeVar

import httpx
from pydantic import BaseModel

from .models import (
    PricingCommitRequest,
    PricingCommitResponse,
    PricingPreviewRequest,
    PricingPreviewResponse,
    PricingReleaseRequest,
    PricingReleaseResponse,
    PricingReserveRequest,
    PricingReserveResponse,
)

T = TypeVar("T", bound=BaseModel)


def _cfg_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clean_bearer(token: str) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _disabled_payload(response_type: Type[T]) -> dict:
    payload = {"status": "disabled", "message": "Pricing disabled"}
    if hasattr(response_type, "model_fields"):
        fields = getattr(response_type, "model_fields", {})
        if "pricing" in fields:
            payload["pricing"] = {
                "state": "disabled",
                "enabled": False,
                "message": "Pricing disabled",
            }
        if "pricing_summary" in fields:
            payload["pricing_summary"] = {}
    return payload


@dataclass(frozen=True)
class PricingClientConfig:
    enabled: bool
    base_url: str
    timeout_s: float
    service_name: str
    bearer_token: str = ""


class PricingClientError(RuntimeError):
    pass


class SvcPricingClient:
    def __init__(self, config: PricingClientConfig) -> None:
        self.config = config

    @classmethod
    def from_env(cls, service_name: str) -> "SvcPricingClient":
        return cls(
            PricingClientConfig(
                enabled=_cfg_bool("DF_PRICING_ENABLED", False),
                base_url=os.getenv("DF_PRICING_URL", "").rstrip("/"),
                timeout_s=float(os.getenv("DF_PRICING_TIMEOUT_S", "10")),
                service_name=service_name,
                bearer_token=_clean_bearer(os.getenv("DF_PRICING_BEARER_TOKEN", "")),
            )
        )

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.config.base_url)

    def _headers(self, user_id: Optional[str] = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Service-Name": self.config.service_name,
        }
        if user_id:
            headers["X-User-Id"] = str(user_id)
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
        return headers

    async def _post(
        self,
        path: str,
        payload: BaseModel,
        response_type: Type[T],
        *,
        user_id: Optional[str] = None,
    ) -> T:
        if not self.enabled:
            return response_type.model_validate(_disabled_payload(response_type))

        url = f"{self.config.base_url}{path}"

        async with httpx.AsyncClient(timeout=self.config.timeout_s, follow_redirects=True) as client:
            response = await client.post(
                url,
                json=payload.model_dump(mode="json", exclude_none=True),
                headers=self._headers(user_id=user_id),
            )

        if response.is_error:
            body = ""
            try:
                body = response.text
            except Exception:
                body = "<unreadable>"
            raise PricingClientError(f"{path} failed: {response.status_code} {body}")

        try:
            data = response.json()
            if isinstance(data, dict):
                return response_type.model_validate(data)
        except Exception:
            pass

        return response_type.model_validate({"status": "ok"})

    async def preview(self, req: PricingPreviewRequest) -> PricingPreviewResponse:
        return await self._post(
            "/api/pricing/reservations/preview",
            req,
            PricingPreviewResponse,
            user_id=req.user_id,
        )

    async def reserve(self, req: PricingReserveRequest) -> PricingReserveResponse:
        return await self._post(
            "/api/pricing/reservations/reserve",
            req,
            PricingReserveResponse,
            user_id=req.user_id,
        )

    async def commit(self, req: PricingCommitRequest) -> PricingCommitResponse:
        return await self._post(
            "/api/pricing/reservations/commit",
            req,
            PricingCommitResponse,
            user_id=req.user_id,
        )

    async def release(self, req: PricingReleaseRequest) -> PricingReleaseResponse:
        return await self._post(
            "/api/pricing/reservations/release",
            req,
            PricingReleaseResponse,
            user_id=req.user_id,
        )
