from __future__ import annotations

from typing import Tuple, Optional, Mapping, Any
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs, unquote

import base64
import os

import httpx
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
    ContentSettings,
)

from app.config import settings


def _ext_for_content_type(content_type: str) -> str:
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct == "image/png":
        return "png"
    if ct == "image/webp":
        return "webp"
    if ct in ("image/jpg", "image/jpeg"):
        return "jpg"
    # default safe
    return "png"


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def _try_parse_container_blob_from_url(url: str) -> Optional[Tuple[str, str]]:
    """
    https://<acct>.blob.core.windows.net/<container>/<blob...>[?sas]
    -> (container, blob)
    """
    try:
        base = _strip_query(url)
        path = urlsplit(base).path.lstrip("/")  # "<container>/<blob...>"
        if "/" not in path:
            return None
        container, blob = path.split("/", 1)
        if not container or not blob:
            return None
        return container, blob
    except Exception:
        return None


def _try_parse_sas_expiry_utc_naive(url: str) -> Optional[datetime]:
    """
    Extracts se=... from SAS querystring, returns a naive UTC datetime.
    Example se=2026-02-14T15%3A26%3A58Z
    """
    try:
        q = urlsplit(url).query
        if not q:
            return None
        qs = parse_qs(q)
        se_vals = qs.get("se") or []
        if not se_vals:
            return None

        se = unquote(se_vals[0])  # "2026-02-14T15:26:58Z"
        # fromisoformat doesn't accept "Z"
        if se.endswith("Z"):
            se = se[:-1] + "+00:00"

        dt = datetime.fromisoformat(se)
        # normalize to UTC naive
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


class AzureStorageService:
    """Azure Blob Storage operations for face images"""

    def __init__(self):
        self.connection_string = settings.AZURE_STORAGE_CONNECTION_STRING
        self.container = settings.FACE_OUTPUT_CONTAINER
        self.blob_service = BlobServiceClient.from_connection_string(self.connection_string)

    async def download_image(self, url: str) -> bytes:
        """Download image from URL"""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            return response.content

    async def upload_image(
        self,
        image_bytes: bytes,
        user_id: str,
        job_id: str,
        variant: int,
        content_type: str = "image/jpeg",
    ) -> Tuple[str, str]:
        """
        Upload image to Azure Blob Storage.

        Returns:
            (storage_path, blob_url_with_sas)
        """
        ext = _ext_for_content_type(content_type)

        # Keep your existing layout; just make extension consistent with actual bytes
        blob_name = f"{user_id}/{job_id}/variant_{variant}.{ext}"

        blob_client = self.blob_service.get_blob_client(container=self.container, blob=blob_name)

        blob_client.upload_blob(
            image_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

        sas_url = self._generate_sas_url(blob_name)
        return blob_name, sas_url

    async def upload_from_url(
        self,
        url: str,
        user_id: str,
        job_id: str,
        variant: int,
    ) -> Tuple[str, str]:
        """
        Upload image from URL or data URL to Azure Blob Storage.
        Supports both HTTP URLs and data: URLs from fal.ai.

        Returns:
            (storage_path, blob_url_with_sas)
        """
        if url.startswith("data:"):
            # data:image/png;base64,....
            header, data = url.split(",", 1)
            image_bytes = base64.b64decode(data)

            content_type = "image/jpeg"
            if "image/" in header:
                type_part = header.split("image/")[1].split(";")[0]
                content_type = f"image/{type_part}"
            return await self.upload_image(image_bytes, user_id, job_id, variant, content_type)

        # HTTP URL
        image_bytes = await self.download_image(url)
        # Best-effort: assume jpeg if not known
        return await self.upload_image(image_bytes, user_id, job_id, variant, "image/jpeg")

    # ---------------------------------------------------------------------
    # Compatibility methods to unblock I2I/T2I pipelines that expect these
    # ---------------------------------------------------------------------

    async def upload_bytes(
        self,
        *,
        data: bytes,
        user_id: str,
        job_id: str,
        variant: int,
        content_type: str = "image/png",
    ) -> Tuple[str, str]:
        """
        Compatibility wrapper for orchestrator paths where providers return raw bytes (OpenAI).
        Delegates to upload_image().
        """
        return await self.upload_image(data, user_id, job_id, variant, content_type)

    async def upload_local_file(
        self,
        *,
        local_path: str,
        user_id: str,
        job_id: str,
        variant: int,
        content_type: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Upload a local file by reading bytes and delegating to upload_image().
        """
        if not content_type:
            # very small inference based on extension
            ext = os.path.splitext(local_path)[1].lower()
            if ext == ".png":
                content_type = "image/png"
            elif ext in (".jpg", ".jpeg"):
                content_type = "image/jpeg"
            elif ext == ".webp":
                content_type = "image/webp"
            else:
                content_type = "image/png"

        with open(local_path, "rb") as f:
            data = f.read()
        return await self.upload_image(data, user_id, job_id, variant, content_type)

    # Alias expected by some codebases
    upload_from_file = upload_local_file

    # ---------------------------------------------------------------------

    def _generate_sas_url(self, blob_name: str, hours: int = 24, container_name: Optional[str] = None) -> str:
        """Generate SAS URL for blob access"""
        container = container_name or self.container

        conn_parts = dict(
            item.split("=", 1) for item in self.connection_string.split(";") if "=" in item
        )
        account_name = conn_parts.get("AccountName")
        account_key = conn_parts.get("AccountKey")

        if not account_name or not account_key:
            raise Exception("Could not parse storage account credentials")

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=hours),
        )

        return f"https://{account_name}.blob.core.windows.net/{container}/{blob_name}?{sas_token}"

    def _resolve_container_and_blob_name(
        self,
        *,
        storage_path_or_url: str,
        meta_json: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[str, str]:
        """
        Resolve (container, blob_name) from:
          - full blob URL (with or without SAS)
          - "container/blob"
          - "blob" (assumed self.container)
          - meta_json storage_container + (blob_name or storage_path)
        """
        s = (storage_path_or_url or "").strip()
        mj = meta_json or {}

        # URL
        if s.startswith("http://") or s.startswith("https://"):
            got = _try_parse_container_blob_from_url(s)
            if got:
                return got

        # meta_json explicit: supports your upload schema
        sc = mj.get("storage_container")
        bn = mj.get("blob_name")
        sp = mj.get("storage_path")

        if isinstance(sc, str) and sc.strip():
            if isinstance(bn, str) and bn.strip():
                return sc.strip(), bn.strip().lstrip("/")
            if isinstance(sp, str) and sp.strip():
                sp2 = _strip_query(sp.strip()).lstrip("/")
                # sp might be "container/blob" or just "blob"
                if "/" in sp2 and not sp2.startswith("http"):
                    c, b = sp2.split("/", 1)
                    if c and b:
                        # if storage_path already has container, use it
                        return c, b
                return sc.strip(), sp2

        # "container/blob"
        if "/" in s and not s.startswith("http"):
            s2 = _strip_query(s).lstrip("/")
            c, b = s2.split("/", 1)
            if c.strip() and b.strip():
                return c.strip(), b.strip().lstrip("/")

        # "blob" only
        if s:
            return self.container, _strip_query(s).lstrip("/")

        raise ValueError("Empty storage_path_or_url and insufficient meta_json to resolve blob")

    async def regenerate_sas_url(self, storage_path: str, hours: int = 24) -> str:
        """
        Regenerate SAS URL for an existing blob.

        Backwards compatible:
          - storage_path can be blob_name ("user/job/variant.png")
          - or "container/blob"
          - or a full blob URL (with or without SAS)
        """
        container, blob_name = self._resolve_container_and_blob_name(storage_path_or_url=storage_path)
        return self._generate_sas_url(blob_name, hours=hours, container_name=container)

    async def get_readonly_sas_url(
        self,
        *,
        storage_ref: Optional[str],
        meta_json: Optional[Mapping[str, Any]] = None,
        hours: int = 24,
        refresh_if_within_minutes: int = 60,
    ) -> Optional[str]:
        """
        Returns a SAS URL suitable for client read. If storage_ref already has a SAS and it
        doesn't expire soon, returns it as-is; otherwise generates a fresh SAS.

        This lets you keep storing SAS in DB today, but guarantees the API returns a valid URL.
        """
        if not storage_ref and not meta_json:
            return None

        now = datetime.utcnow()

        if storage_ref and "?" in storage_ref:
            exp = _try_parse_sas_expiry_utc_naive(storage_ref)
            # if expiry parses and is far enough away, keep it
            if exp and exp > (now + timedelta(minutes=refresh_if_within_minutes)):
                return storage_ref
            # else refresh using URL parse/meta_json
            container, blob_name = self._resolve_container_and_blob_name(
                storage_path_or_url=storage_ref,
                meta_json=meta_json,
            )
            return self._generate_sas_url(blob_name, hours=hours, container_name=container)

        # No SAS in storage_ref; resolve and generate
        if storage_ref:
            container, blob_name = self._resolve_container_and_blob_name(
                storage_path_or_url=storage_ref,
                meta_json=meta_json,
            )
            return self._generate_sas_url(blob_name, hours=hours, container_name=container)

        # No storage_ref; rely on meta_json
        container, blob_name = self._resolve_container_and_blob_name(
            storage_path_or_url="",
            meta_json=meta_json,
        )
        return self._generate_sas_url(blob_name, hours=hours, container_name=container)