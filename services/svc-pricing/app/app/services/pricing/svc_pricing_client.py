# services/<svc>/app/app/services/pricing/svc_pricing_client.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

import httpx


def _norm_bearer(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    return t if t.lower().startswith("bearer ") else f"Bearer {t}"


@dataclass(frozen=True)
class PricingReservation:
    reservation_id: UUID
    status: str
    expires_at: str
    reserved_credits: int
    currency: str
    variant_code: str
    category: str
    billing_mode_snapshot: str
    hold_applied: bool
    quote_breakdown: Dict[str, Any]


class SvcPricingClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 15.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def _headers(self, *, bearer_token: str, user_id: UUID, country_code: Optional[str] = None) -> Dict[str, str]:
        h = {
            "Authorization": _norm_bearer(bearer_token),
            "X-User-Id": str(user_id),
            "Content-Type": "application/json",
        }
        if country_code:
            h["X-Country-Code"] = country_code
        return h

    async def _request(self, method: str, path: str, *, headers: Dict[str, str], json_body: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    resp = await client.request(method, url, headers=headers, json=json_body)
                if resp.status_code in (502, 503, 504):
                    raise httpx.HTTPStatusError("transient", request=resp.request, response=resp)
                if resp.status_code >= 400:
                    # bubble up detail for caller
                    try:
                        data = resp.json()
                    except Exception:
                        data = {"detail": resp.text}
                    raise RuntimeError(f"pricing_http_{resp.status_code}:{data.get('detail')}")
                return resp.json()
            except Exception as e:
                last_exc = e
                if attempt < self.max_retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise

        raise last_exc or RuntimeError("pricing_request_failed")

    async def quote(
        self,
        *,
        bearer_token: str,
        user_id: UUID,
        variant_code: str,
        params: Dict[str, Any],
        channel: str = "web",
        currency: Optional[str] = None,
        country_code: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/api/pricing/quote",
            headers=self._headers(bearer_token=bearer_token, user_id=user_id, country_code=country_code),
            json_body={
                "variant_code": variant_code,
                "params": params or {},
                "channel": channel,
                "currency": currency,
                "country_code": country_code,
            },
        )

    async def reserve(
        self,
        *,
        bearer_token: str,
        user_id: UUID,
        idempotency_key: str,
        variant_code: str,
        params: Dict[str, Any],
        channel: str = "web",
        currency: Optional[str] = None,
        country_code: Optional[str] = None,
        job_ref: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/api/credits/reserve",
            headers=self._headers(bearer_token=bearer_token, user_id=user_id, country_code=country_code),
            json_body={
                "idempotency_key": idempotency_key,
                "variant_code": variant_code,
                "params": params or {},
                "channel": channel,
                "currency": currency,
                "country_code": country_code,
                "job_ref": job_ref,
                "ttl_seconds": ttl_seconds,
            },
        )

    async def get_reservation(
        self,
        *,
        bearer_token: str,
        user_id: UUID,
        reservation_id: UUID,
        include_quote: bool = True,
        country_code: Optional[str] = None,
    ) -> PricingReservation:
        data = await self._request(
            "GET",
            f"/api/credits/reservations/{reservation_id}?include_quote={'true' if include_quote else 'false'}",
            headers=self._headers(bearer_token=bearer_token, user_id=user_id, country_code=country_code),
        )
        return PricingReservation(
            reservation_id=UUID(str(data["reservation_id"])),
            status=str(data.get("status") or ""),
            expires_at=str(data.get("expires_at") or ""),
            reserved_credits=int(data.get("reserved_credits") or 0),
            currency=str(data.get("currency") or ""),
            variant_code=str(data.get("variant_code") or ""),
            category=str(data.get("category") or ""),
            billing_mode_snapshot=str(data.get("billing_mode_snapshot") or ""),
            hold_applied=bool(data.get("hold_applied") or False),
            quote_breakdown=dict(data.get("quote_breakdown") or {}),
        )

    async def finalize(
        self,
        *,
        bearer_token: str,
        user_id: UUID,
        reservation_id: UUID,
        finalize_idempotency_key: str,
        actuals: Optional[Dict[str, Any]] = None,
        channel: str = "web",
        country_code: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/api/credits/finalize",
            headers=self._headers(bearer_token=bearer_token, user_id=user_id, country_code=country_code),
            json_body={
                "reservation_id": str(reservation_id),
                "finalize_idempotency_key": finalize_idempotency_key,
                "actuals": actuals or {},
                "channel": channel,
                "country_code": country_code,
            },
        )

    async def release(
        self,
        *,
        bearer_token: str,
        user_id: UUID,
        reservation_id: UUID,
        reason: str = "job_failed",
        channel: str = "web",
        country_code: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/api/credits/release",
            headers=self._headers(bearer_token=bearer_token, user_id=user_id, country_code=country_code),
            json_body={
                "reservation_id": str(reservation_id),
                "reason": reason,
                "channel": channel,
                "country_code": country_code,
            },
        )