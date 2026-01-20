from __future__ import annotations

from typing import Tuple, Optional
from datetime import datetime, timedelta
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

    def _generate_sas_url(self, blob_name: str, hours: int = 24) -> str:
        """Generate SAS URL for blob access"""
        conn_parts = dict(
            item.split("=", 1) for item in self.connection_string.split(";") if "=" in item
        )
        account_name = conn_parts.get("AccountName")
        account_key = conn_parts.get("AccountKey")

        if not account_name or not account_key:
            raise Exception("Could not parse storage account credentials")

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=self.container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=hours),
        )

        return f"https://{account_name}.blob.core.windows.net/{self.container}/{blob_name}?{sas_token}"

    async def regenerate_sas_url(self, storage_path: str, hours: int = 24) -> str:
        """Regenerate SAS URL for existing blob"""
        return self._generate_sas_url(storage_path, hours)