from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse

from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
    BlobSasPermissions,
)

from app.config import settings


DEFAULT_MUSIC_INPUT_CONTAINER = "music-input"
DEFAULT_MUSIC_OUTPUT_CONTAINER = "music-output"


@dataclass(frozen=True)
class UploadBytesResult:
    container: str
    storage_path: str
    sas_url: str
    bytes: int
    sha256: str


def _truthy(x: object) -> bool:
    if x is True:
        return True
    if x is False or x is None:
        return False
    if isinstance(x, (int, float)):
        return x != 0
    if isinstance(x, str):
        return x.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(x)


def _file_sha256_and_size(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


class AzureStorageService:
    """
    Azure Blob Storage operations for svc-music.

    Containers (recommended):
      - music-input  : user uploads (BYO song audio, voice references)
      - music-output : generated artifacts (full_mix, stems, previews, videos)

    Default upload pattern (blob path):
      {user_id}/{scope_id}/variant_{N}.{ext}

    Requires:
      settings.AZURE_STORAGE_CONNECTION_STRING
    Optional:
      settings.MUSIC_INPUT_CONTAINER  (default "music-input")
      settings.MUSIC_OUTPUT_CONTAINER (default "music-output")
      settings.MUSIC_SAS_HOURS (default 24)
      settings.AZURE_STORAGE_AUTO_CREATE_CONTAINER (default True)
    """

    def __init__(self, *, container: Optional[str] = None):
        self.connection_string = (getattr(settings, "AZURE_STORAGE_CONNECTION_STRING", None) or "").strip()
        if not self.connection_string:
            raise RuntimeError("missing_azure_storage_connection_string")

        default_container = (getattr(settings, "MUSIC_OUTPUT_CONTAINER", None) or DEFAULT_MUSIC_OUTPUT_CONTAINER).strip()
        self.container = (container or default_container).strip()
        if not self.container:
            raise RuntimeError("missing_music_container")

        try:
            self.sas_hours = int(getattr(settings, "MUSIC_SAS_HOURS", 24))
        except Exception:
            self.sas_hours = 24
        if self.sas_hours <= 0:
            self.sas_hours = 24

        self.blob_service = BlobServiceClient.from_connection_string(self.connection_string)

        parts = self._parse_connection_string(self.connection_string)
        self.account_name = (getattr(self.blob_service, "account_name", None) or parts.get("AccountName") or "").strip()
        self.account_key = (parts.get("AccountKey") or "").strip()

        if not self.account_name or not self.account_key:
            # If your connection string is SAS-only (SharedAccessSignature), you can't mint SAS here.
            raise RuntimeError("could_not_parse_storage_account_credentials")

        self._container_client = self.blob_service.get_container_client(self.container)

        auto_create = _truthy(getattr(settings, "AZURE_STORAGE_AUTO_CREATE_CONTAINER", True))
        if auto_create:
            self._ensure_container_exists_best_effort()

    @classmethod
    def for_input(cls) -> "AzureStorageService":
        c = (getattr(settings, "MUSIC_INPUT_CONTAINER", None) or DEFAULT_MUSIC_INPUT_CONTAINER).strip()
        return cls(container=c)

    @classmethod
    def for_output(cls) -> "AzureStorageService":
        c = (getattr(settings, "MUSIC_OUTPUT_CONTAINER", None) or DEFAULT_MUSIC_OUTPUT_CONTAINER).strip()
        return cls(container=c)

    @staticmethod
    def _parse_connection_string(cs: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for item in (cs or "").split(";"):
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            k = (k or "").strip()
            v = (v or "").strip()
            if k:
                out[k] = v
        return out

    def _ensure_container_exists_best_effort(self) -> None:
        try:
            self._container_client.get_container_properties()
            return
        except Exception:
            pass
        try:
            self._container_client.create_container()
        except Exception:
            pass

    @staticmethod
    def _clean_path(p: str) -> str:
        """
        Normalize blob path:
        - no leading slash
        - normalize backslashes
        - remove '.', '..' segments
        """
        s = (p or "").strip().replace("\\", "/").lstrip("/")
        if not s:
            return ""
        segments = []
        for seg in s.split("/"):
            seg = (seg or "").strip()
            if not seg or seg in (".", ".."):
                continue
            segments.append(seg)
        return "/".join(segments)

    @staticmethod
    def parse_blob_url(url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse:
          https://{acct}.blob.core.windows.net/{container}/{blob_path}?{sas}
        Returns: (container, blob_path)
        """
        try:
            if not url:
                return (None, None)
            u = urlparse(url)
            path = (u.path or "").lstrip("/")  # "{container}/{blob_path}"
            if not path:
                return (None, None)
            parts = path.split("/", 1)
            if len(parts) != 2:
                return (None, None)
            container = parts[0].strip() or None
            blob_path = parts[1].lstrip("/") or None
            return (container, blob_path)
        except Exception:
            return (None, None)

    @staticmethod
    def build_storage_path(*, user_id: str, scope_id: str, variant: int, ext: str) -> str:
        """
        Build canonical storage path:
          {user_id}/{scope_id}/variant_{variant}.{ext}
        """
        uid = AzureStorageService._clean_path(str(user_id))
        sid = AzureStorageService._clean_path(str(scope_id))
        v = int(variant or 1)
        if v < 1:
            v = 1
        e = (ext or "").lstrip(".").strip().lower() or "bin"
        return AzureStorageService._clean_path(f"{uid}/{sid}/variant_{v}.{e}")

    @staticmethod
    def build_music_job_path(*, user_id: str, project_id: str, job_id: str, filename: str) -> str:
        """
        Canonical music-output job path:
          {user_id}/{project_id}/{job_id}/{filename}
        """
        uid = AzureStorageService._clean_path(str(user_id))
        pid = AzureStorageService._clean_path(str(project_id))
        jid = AzureStorageService._clean_path(str(job_id))
        fname = AzureStorageService._clean_path(str(filename or "").strip()) or "artifact.bin"

        if not uid or not pid or not jid:
            raise ValueError("user_id, project_id, job_id are required")

        return AzureStorageService._clean_path(f"{uid}/{pid}/{jid}/{fname}")

    def sas_url_for(self, storage_path: str) -> str:
        storage_path = self._clean_path(storage_path)
        if not storage_path:
            raise ValueError("storage_path is required")

        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=5)  # clock-skew hardening
        expiry = now + timedelta(hours=self.sas_hours)

        sas_token = generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container,
            blob_name=storage_path,
            account_key=self.account_key,
            permission=BlobSasPermissions(read=True),
            start=start,
            expiry=expiry,
        )
        return f"https://{self.account_name}.blob.core.windows.net/{self.container}/{storage_path}?{sas_token}"

    def _sync_upload_blob(self, *, blob_name: str, data: bytes, content_type: str) -> None:
        blob_client = self.blob_service.get_blob_client(container=self.container, blob=blob_name)
        blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

    def _sync_upload_file(self, *, blob_name: str, local_path: Path, content_type: str) -> None:
        blob_client = self.blob_service.get_blob_client(container=self.container, blob=blob_name)
        with local_path.open("rb") as f:
            blob_client.upload_blob(
                f,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )

    async def upload_bytes_to_path(
        self,
        *,
        data: bytes,
        storage_path: str,
        content_type: str = "application/octet-stream",
    ) -> UploadBytesResult:
        if data is None:
            raise ValueError("data is required")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")

        content_type = (content_type or "").strip() or "application/octet-stream"
        blob_name = self._clean_path(storage_path)
        if not blob_name:
            raise ValueError("invalid_blob_name")

        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)

        await asyncio.to_thread(self._sync_upload_blob, blob_name=blob_name, data=data, content_type=content_type)

        return UploadBytesResult(
            container=self.container,
            storage_path=blob_name,
            sas_url=self.sas_url_for(blob_name),
            bytes=size,
            sha256=sha256,
        )

    async def upload_bytes(
        self,
        *,
        data: bytes,
        user_id: str,
        scope_id: str,
        variant: int = 1,
        ext: str = "bin",
        content_type: str = "application/octet-stream",
    ) -> UploadBytesResult:
        if data is None:
            raise ValueError("data is required")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")

        content_type = (content_type or "").strip() or "application/octet-stream"

        blob_name = self.build_storage_path(
            user_id=str(user_id),
            scope_id=str(scope_id),
            variant=int(variant or 1),
            ext=str(ext or "bin"),
        )
        if not blob_name:
            raise ValueError("invalid_blob_name")

        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)

        await asyncio.to_thread(self._sync_upload_blob, blob_name=blob_name, data=data, content_type=content_type)

        return UploadBytesResult(
            container=self.container,
            storage_path=blob_name,
            sas_url=self.sas_url_for(blob_name),
            bytes=size,
            sha256=sha256,
        )

    async def upload_file_to_path(
        self,
        *,
        local_path: str | Path,
        storage_path: str,
        content_type: str = "application/octet-stream",
    ) -> UploadBytesResult:
        p = Path(local_path)
        if not p.exists() or not p.is_file():
            raise ValueError(f"local_file_not_found: {p}")

        content_type = (content_type or "").strip() or "application/octet-stream"
        blob_name = self._clean_path(storage_path)
        if not blob_name:
            raise ValueError("invalid_blob_name")

        sha256, size = await asyncio.to_thread(_file_sha256_and_size, p)

        await asyncio.to_thread(self._sync_upload_file, blob_name=blob_name, local_path=p, content_type=content_type)

        return UploadBytesResult(
            container=self.container,
            storage_path=blob_name,
            sas_url=self.sas_url_for(blob_name),
            bytes=size,
            sha256=sha256,
        )

    async def upload_file(
        self,
        *,
        local_path: str | Path,
        user_id: str,
        scope_id: str,
        variant: int = 1,
        ext: str = "bin",
        content_type: str = "application/octet-stream",
    ) -> UploadBytesResult:
        blob_name = self.build_storage_path(
            user_id=str(user_id),
            scope_id=str(scope_id),
            variant=int(variant or 1),
            ext=str(ext or "bin"),
        )
        return await self.upload_file_to_path(local_path=local_path, storage_path=blob_name, content_type=content_type)

    async def upload_music_output_file(
        self,
        *,
        user_id: str,
        project_id: str,
        job_id: str,
        local_path: str | Path,
        content_type: str,
        blob_filename: str,
    ) -> UploadBytesResult:
        """
        Generic helper for job-scoped artifacts in the current container (intended for music-output).

        Storage path:
          {user_id}/{project_id}/{job_id}/{blob_filename}
        """
        storage_path = self.build_music_job_path(
            user_id=str(user_id),
            project_id=str(project_id),
            job_id=str(job_id),
            filename=str(blob_filename or "artifact.bin"),
        )
        return await self.upload_file_to_path(local_path=local_path, storage_path=storage_path, content_type=content_type)

    async def upload_music_fallback_audio_and_get_sas_url(
        self,
        *,
        user_id: str,
        project_id: str,
        job_id: str,
        local_path: str | Path,
        content_type: str = "audio/wav",
        blob_filename: str = "fallback_full_mix.wav",
    ) -> str:
        """
        Back-compat wrapper used by svc-music orchestration/autopilot providers.

        Storage path:
          {user_id}/{project_id}/{job_id}/{blob_filename}

        Returns:
          read SAS URL
        """
        res = await self.upload_music_output_file(
            user_id=user_id,
            project_id=project_id,
            job_id=job_id,
            local_path=local_path,
            content_type=content_type,
            blob_filename=blob_filename,
        )
        return res.sas_url