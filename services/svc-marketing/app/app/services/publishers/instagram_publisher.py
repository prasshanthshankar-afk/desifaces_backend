from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.services.secrets.secret_provider import DefaultSecretProvider
from app.services.orchestration.utils.config import cfg_int, cfg_str


@dataclass
class PublishResult:
    ok: bool
    media_id: Optional[str] = None
    permalink: Optional[str] = None
    creation_id: Optional[str] = None
    error: Optional[str] = None


class InstagramPublisher:
    """
    Publishes Instagram Reels via Instagram Graph API.

    Key points:
    - Do NOT persist short-lived tokens anywhere.
    - Prefer secret-based long-lived token fetched at runtime.
    - For Reels: create container -> wait FINISHED -> publish -> fetch permalink.
    """

    def __init__(self, secrets: Optional[DefaultSecretProvider] = None) -> None:
        self.secrets = secrets or DefaultSecretProvider()

        # Prefer MARKETING_* envs; fall back to legacy settings.*
        self.graph_version = cfg_str("MARKETING_IG_GRAPH_VERSION", "") or "v23.0"
        self.ig_user_id = (
            cfg_str("MARKETING_IG_USER_ID", "").strip()
            or cfg_str("MARKETING_IG_BUSINESS_ACCOUNT_ID", "").strip()
            or str(getattr(settings, "IG_BUSINESS_ACCOUNT_ID", "") or "").strip()
        )

        if not self.ig_user_id:
            raise RuntimeError("MARKETING_IG_USER_ID (or IG_BUSINESS_ACCOUNT_ID) is required")

        # Token: secret preferred; env fallback
        self.token_secret = cfg_str("MARKETING_IG_ACCESS_TOKEN_SECRET", "").strip()
        self.token_env = cfg_str("MARKETING_IG_ACCESS_TOKEN", "").strip() or str(getattr(settings, "IG_ACCESS_TOKEN", "") or "").strip()

        if not (self.token_secret or self.token_env):
            raise RuntimeError("MARKETING_IG_ACCESS_TOKEN_SECRET (preferred) or MARKETING_IG_ACCESS_TOKEN is required")

    def _token(self) -> str:
        """
        Resolve token at runtime.
        DO NOT store short-lived tokens in DB.
        """
        if self.token_secret:
            getter = getattr(self.secrets, "get_secret", None) or getattr(self.secrets, "get", None)
            if callable(getter):
                v = getter(self.token_secret)
                tok = str(v or "").strip()
                if tok:
                    return tok
        return self.token_env

    async def _post(self, client: httpx.AsyncClient, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"https://graph.facebook.com/{self.graph_version}{path}"
        payload = dict(data)
        payload["access_token"] = self._token()
        r = await client.post(url, data=payload)
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text}
        return {"http": r.status_code, "json": j}

    async def _get(self, client: httpx.AsyncClient, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"https://graph.facebook.com/{self.graph_version}{path}"
        q = dict(params)
        q["access_token"] = self._token()
        r = await client.get(url, params=q)
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text}
        return {"http": r.status_code, "json": j}

    async def _wait_container(self, client: httpx.AsyncClient, creation_id: str, timeout_s: int = 300) -> tuple[bool, str]:
        """
        Wait until status_code == FINISHED.
        """
        deadline = asyncio.get_event_loop().time() + float(timeout_s)
        last = ""
        while asyncio.get_event_loop().time() < deadline:
            res = await self._get(client, f"/{creation_id}", {"fields": "status_code"})
            j = res.get("json") if isinstance(res, dict) else None
            status = str(j.get("status_code") or "").upper() if isinstance(j, dict) else ""
            last = status or last

            if status == "FINISHED":
                return True, status
            if status in ("ERROR", "FAILED", "EXPIRED"):
                return False, status

            await asyncio.sleep(2.0)
        return False, last or "TIMEOUT"

    async def publish_reel(self, video_url: str, caption: str) -> PublishResult:
        """
        Publish a Reel from a publicly accessible video URL.
        NOTE: video_url must remain accessible while Meta fetches it.
        """
        try:
            timeout_s = cfg_int("MARKETING_IG_TIMEOUT_S", 600)
            wait_s = cfg_int("MARKETING_IG_CONTAINER_WAIT_S", 300)

            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
                # 1) Create container
                create = await self._post(
                    client,
                    f"/{self.ig_user_id}/media",
                    {"media_type": "REELS", "video_url": video_url, "caption": caption},
                )
                if int(create.get("http") or 0) >= 300:
                    return PublishResult(ok=False, error=f"IG create failed: {create}")

                creation_id = str((create.get("json") or {}).get("id") or "")
                if not creation_id:
                    return PublishResult(ok=False, error=f"Missing creation_id: {create}")

                # 2) Wait until FINISHED (Meta downloads your video_url here)
                ok, status = await self._wait_container(client, creation_id, timeout_s=wait_s)
                if not ok:
                    return PublishResult(ok=False, creation_id=creation_id, error=f"IG container not ready: {status}")

                # 3) Publish container
                pub = await self._post(
                    client,
                    f"/{self.ig_user_id}/media_publish",
                    {"creation_id": creation_id},
                )
                if int(pub.get("http") or 0) >= 300:
                    return PublishResult(ok=False, creation_id=creation_id, error=f"IG publish failed: {pub}")

                media_id = str((pub.get("json") or {}).get("id") or "")
                if not media_id:
                    return PublishResult(ok=False, creation_id=creation_id, error=f"Missing media_id: {pub}")

                # 4) Fetch permalink
                link = await self._get(client, f"/{media_id}", {"fields": "permalink"})
                permalink = None
                if int(link.get("http") or 0) < 300:
                    permalink = (link.get("json") or {}).get("permalink")

                return PublishResult(ok=True, media_id=media_id, permalink=permalink, creation_id=creation_id)

        except Exception as e:
            return PublishResult(ok=False, error=str(e))