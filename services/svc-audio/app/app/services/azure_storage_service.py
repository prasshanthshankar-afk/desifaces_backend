from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
    BlobSasPermissions,
)

from app.config import settings


@dataclass(frozen=True)
class UploadBytesResult:
    storage_path: str
    sas_url: str
    bytes: int
    sha256: str


class AzureStorageService:
    """
    Azure Blob Storage operations for svc-audio.

    Upload pattern:
      {user_id}/{job_id}/variant_{N}.{ext}

    Requires:
      settings.AZURE_STORAGE_CONNECTION_STRING
      settings.AUDIO_OUTPUT_CONTAINER
    """

    def __init__(self):
        self.connection_string = settings.AZURE_STORAGE_CONNECTION_STRING.strip()
        if not self.connection_string:
            raise RuntimeError("missing_azure_storage_connection_string")

        self.audio_container = settings.AUDIO_OUTPUT_CONTAINER
        self.sas_hours = int(getattr(settings, "AUDIO_SAS_HOURS", 24))

        self.blob_service = BlobServiceClient.from_connection_string(self.connection_string)

        parts = dict(item.split("=", 1) for item in self.connection_string.split(";") if "=" in item)
        self.account_name = parts.get("AccountName")
        self.account_key = parts.get("AccountKey")
        if not self.account_name or not self.account_key:
            raise RuntimeError("could_not_parse_storage_account_credentials")

        # Make sure container exists (safe in dev; idempotent-ish)
        container_client = self.blob_service.get_container_client(self.audio_container)
        try:
            container_client.get_container_properties()
        except Exception:
            # Container doesn't exist or not accessible; try create.
            # If it already exists due to race, Azure will throw; ignore that.
            try:
                container_client.create_container()
            except Exception:
                pass

    async def upload_bytes(
        self,
        *,
        data: bytes,
        user_id: str,
        job_id: str,
        variant: int = 1,
        ext: str = "wav",
        content_type: str = "audio/wav",
    ) -> UploadBytesResult:
        """
        Upload bytes to AUDIO_OUTPUT_CONTAINER.
        """
        ext = (ext or "").lstrip(".").strip().lower() or "wav"
        content_type = (content_type or "").strip() or "application/octet-stream"

        blob_name = f"{user_id}/{job_id}/variant_{variant}.{ext}"
        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)

        def _sync_upload() -> None:
            blob_client = self.blob_service.get_blob_client(container=self.audio_container, blob=blob_name)
            blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )

        await asyncio.to_thread(_sync_upload)

        sas_token = generate_blob_sas(
            account_name=self.account_name,
            container_name=self.audio_container,
            blob_name=blob_name,
            account_key=self.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=self.sas_hours),
        )
        sas_url = f"https://{self.account_name}.blob.core.windows.net/{self.audio_container}/{blob_name}?{sas_token}"

        return UploadBytesResult(
            storage_path=blob_name,
            sas_url=sas_url,
            bytes=size,
            sha256=sha256,
        )