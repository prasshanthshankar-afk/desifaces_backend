# services/svc-marketing/app/app/services/publishers/youtube_publisher.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

import httpx

from app.repos.marketing_platform_accounts_repo import MarketingPlatformAccountsRepo
from app.services.secrets.secret_provider import SecretProvider, DefaultSecretProvider
from app.services.rendering.video_variant_service import render_variant


@dataclass
class YouTubePublishResult:
    ok: bool
    video_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None
    raw: Dict[str, Any] = None


class YouTubePublisher:
    """
    Uploads to YouTube using OAuth refresh token + resumable upload.
    - Requires a MarketingPlatformAccount row with platform='youtube'
      and refs for client secret + refresh token.
    """

    def __init__(self, accounts_repo: MarketingPlatformAccountsRepo, secrets: Optional[SecretProvider] = None):
        self.accounts_repo = accounts_repo
        self.secrets = secrets or DefaultSecretProvider()

    async def publish_short(
        self,
        platform_account_id: Optional[UUID],
        video_source_url: str,
        title: str,
        description: str,
        tags: Optional[list[str]] = None,
        privacy_status: str = "public",   # public|unlisted|private
        thumbnail_path: Optional[str] = None,  # local file path (optional)
        work_dir: str = "/tmp/df_marketing_youtube",
    ) -> YouTubePublishResult:
        return await self._publish_video(
            platform_account_id=platform_account_id,
            video_source_url=video_source_url,
            title=title,
            description=description,
            tags=tags or [],
            privacy_status=privacy_status,
            variant="yt_short",
            thumbnail_path=thumbnail_path,
            work_dir=work_dir,
        )

    async def publish_long(
        self,
        platform_account_id: Optional[UUID],
        video_source_url: str,
        title: str,
        description: str,
        tags: Optional[list[str]] = None,
        privacy_status: str = "public",
        thumbnail_path: Optional[str] = None,
        work_dir: str = "/tmp/df_marketing_youtube",
    ) -> YouTubePublishResult:
        return await self._publish_video(
            platform_account_id=platform_account_id,
            video_source_url=video_source_url,
            title=title,
            description=description,
            tags=tags or [],
            privacy_status=privacy_status,
            variant="yt_long",
            thumbnail_path=thumbnail_path,
            work_dir=work_dir,
        )

    async def _publish_video(
        self,
        platform_account_id: Optional[UUID],
        video_source_url: str,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str,
        variant: str,
        thumbnail_path: Optional[str],
        work_dir: str,
    ) -> YouTubePublishResult:
        try:
            acct = None
            if platform_account_id:
                acct = await self.accounts_repo.get_account(platform_account_id)
            if not acct:
                acct = await self.accounts_repo.get_default_account("youtube")
            if not acct:
                return YouTubePublishResult(ok=False, error="No enabled marketing_platform_accounts row for platform=youtube", raw={})

            client_id = (acct["oauth_client_id"] or "").strip()
            if not client_id:
                return YouTubePublishResult(ok=False, error="oauth_client_id missing in platform account", raw={})

            sec = await self.secrets.resolve(acct["oauth_client_secret_ref"])
            ref = await self.secrets.resolve(acct["oauth_refresh_token_ref"])
            if not sec or not ref:
                return YouTubePublishResult(ok=False, error="Missing client_secret or refresh_token (secret refs not resolved)", raw={})

            access_token = await self._get_access_token(
                client_id=client_id,
                client_secret=sec.value,
                refresh_token=ref.value,
            )

            os.makedirs(work_dir, exist_ok=True)

            # 1) download source video to local
            src_path = os.path.join(work_dir, "source.mp4")
            await self._download_to_file(video_source_url, src_path)

            # 2) normalize to yt_short/yt_long
            norm_path = os.path.join(work_dir, f"{variant}.mp4")
            render_variant(src_path, norm_path, variant=variant)  # ffmpeg

            # 3) resumable upload
            video_id = await self._resumable_upload(
                access_token=access_token,
                video_path=norm_path,
                title=title,
                description=description,
                tags=tags,
                privacy_status=privacy_status,
            )

            # 4) optional thumbnail upload (best effort)
            if thumbnail_path and os.path.exists(thumbnail_path):
                try:
                    await self._upload_thumbnail(access_token=access_token, video_id=video_id, image_path=thumbnail_path)
                except Exception:
                    pass

            url = f"https://youtu.be/{video_id}"
            return YouTubePublishResult(ok=True, video_id=video_id, url=url, raw={"variant": variant})

        except Exception as e:
            return YouTubePublishResult(ok=False, error=str(e), raw={})

    async def _get_access_token(self, client_id: str, client_secret: str, refresh_token: str) -> str:
        token_url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(token_url, data=data)
            r.raise_for_status()
            j = r.json()
            tok = j.get("access_token")
            if not tok:
                raise RuntimeError(f"Token refresh failed: {j}")
            return tok

    async def _download_to_file(self, url: str, out_path: str) -> None:
        # Supports file:// for local paths
        if url.startswith("file://"):
            src = url.replace("file://", "", 1)
            with open(src, "rb") as fsrc, open(out_path, "wb") as fdst:
                fdst.write(fsrc.read())
            return

        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)

    async def _resumable_upload(
        self,
        access_token: str,
        video_path: str,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str,
    ) -> str:
        upload_init = "https://www.googleapis.com/upload/youtube/v3/videos"
        params = {"uploadType": "resumable", "part": "snippet,status"}

        size = os.path.getsize(video_path)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        }
        body = {
            "snippet": {
                "title": title[:100],  # YT title limit is higher; keep safe
                "description": description[:5000],
                "tags": tags[:30],
                "categoryId": "22",  # People & Blogs (safe default); adjust later
            },
            "status": {"privacyStatus": privacy_status},
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(upload_init, params=params, headers=headers, json=body)
            r.raise_for_status()
            location = r.headers.get("Location")
            if not location:
                raise RuntimeError("Missing resumable upload Location header")

            # Upload bytes (single PUT; good for shorts; still resumable endpoint)
            with open(video_path, "rb") as f:
                put_headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(size),
                }
                r2 = await client.put(location, headers=put_headers, content=f.read(), timeout=300)
                r2.raise_for_status()
                j = r2.json()
                vid = j.get("id")
                if not vid:
                    raise RuntimeError(f"Upload completed but no video id returned: {j}")
                return vid

    async def _upload_thumbnail(self, access_token: str, video_id: str, image_path: str) -> None:
        url = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
        params = {"videoId": video_id, "uploadType": "media"}
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "image/png"}
        async with httpx.AsyncClient(timeout=120) as client:
            with open(image_path, "rb") as f:
                r = await client.post(url, params=params, headers=headers, content=f.read())
                r.raise_for_status()