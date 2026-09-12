from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

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

    def _resolve_read_coordinates(self, storage_path: str) -> tuple[str, str]:
        """Resolve a durable Audio storage reference into (container, blob).

        Historical Audio rows exist in more than one durable representation:
        - bare blob name: ``user/job/variant_1.mp3``
        - container-prefixed path: ``audio-output-v3/user/job/variant_1.mp3``
        - canonical Azure ref: ``azure://audio-output-v3/user/job/variant_1.mp3``
        - Azure Blob URL without/with an expired SAS token

        The durable value is never treated as a ready-to-use URL.  We extract
        only the container/blob identity and mint a new read-only SAS.
        """
        raw = str(storage_path or "").strip()
        if not raw:
            raise RuntimeError("missing_audio_storage_path")

        default_container = str(self.audio_container or "").strip().strip("/")
        if not default_container:
            raise RuntimeError("missing_audio_output_container")

        if raw.startswith("az://") or raw.startswith("azure://"):
            prefix = "azure://" if raw.startswith("azure://") else "az://"
            remainder = raw[len(prefix):].lstrip("/")
            if "/" not in remainder:
                raise RuntimeError("invalid_audio_azure_storage_ref")
            container, blob_name = remainder.split("/", 1)
        elif raw.startswith("https://") or raw.startswith("http://"):
            parsed = urlparse(raw)
            parts = [part for part in (parsed.path or "").split("/") if part]
            if len(parts) < 2:
                raise RuntimeError("invalid_audio_blob_url")
            container, blob_name = parts[0], "/".join(parts[1:])
        else:
            normalized = raw.lstrip("/")
            default_prefix = f"{default_container}/"
            if normalized.startswith(default_prefix):
                container = default_container
                blob_name = normalized[len(default_prefix):]
            else:
                container = default_container
                blob_name = normalized

        container = str(container or "").strip().strip("/")
        blob_name = str(blob_name or "").strip().lstrip("/")
        if not container or not blob_name:
            raise RuntimeError("invalid_audio_storage_coordinates")

        return container, blob_name

    def generate_read_url(self, storage_path: str, *, hours: int | None = None) -> str:
        """Generate a fresh owner-service read URL for an existing Audio blob.

        ``storage_path`` is a durable blob identity stored in
        ``media_assets.storage_ref``. Historical records can contain bare blob
        names or ``azure://container/blob`` references, so resolve the durable
        coordinates before signing. Never persist this SAS URL as the durable
        identity; callers should request a new URL on read/resume/download.
        """
        container, blob_name = self._resolve_read_coordinates(storage_path)

        ttl_hours = int(hours if hours is not None else self.sas_hours)
        if ttl_hours <= 0:
            raise RuntimeError("invalid_audio_sas_hours")

        sas_token = generate_blob_sas(
            account_name=self.account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=self.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        )
        return f"https://{self.account_name}.blob.core.windows.net/{container}/{blob_name}?{sas_token}"

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

        sas_url = self.generate_read_url(blob_name)

        return UploadBytesResult(
            storage_path=blob_name,
            sas_url=sas_url,
            bytes=size,
            sha256=sha256,
        )
