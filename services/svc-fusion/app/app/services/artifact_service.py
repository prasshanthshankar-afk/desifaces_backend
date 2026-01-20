from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import asyncio
import hashlib
import os
import tempfile

import httpx
from azure.storage.blob import ContentSettings

from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

from app.config import settings


def _json_to_dict(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    try:
        d = dict(val)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _parse_container_blob_from_url(url: str) -> Tuple[str, str]:
    """
    Parse: https://<acct>.blob.core.windows.net/<container>/<blobpath>?...
    -> (container, blobpath)
    """
    p = urlparse(url)
    parts = [x for x in (p.path or "").split("/") if x]
    if len(parts) < 2:
        raise ValueError(f"cannot_parse_blob_url: {url}")
    container = parts[0]
    blob = "/".join(parts[1:])
    return container, blob


def _parse_storage_path(storage_path: str) -> Tuple[str, str]:
    """
    Accepts either:
      - "<container>/<blobpath>"
      - "az://<container>/<blobpath>"
      - "/<container>/<blobpath>"
    """
    s = (storage_path or "").strip()
    if not s:
        raise ValueError("storage_path_empty")

    if s.startswith("az://"):
        s = s[len("az://") :]

    s = s.lstrip("/")
    parts = s.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"invalid_storage_path: {storage_path}")
    return parts[0], parts[1]


def _looks_like_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except Exception:
        return False


@dataclass
class SasConfig:
    account_name: str
    account_key: str


class ArtifactService:
    """
    Phase-1:
      - store provider URLs directly as artifacts.
    Phase-2:
      - copy provider video into Azure Blob and store az:// ref + SAS.

    Extended:
      - mint fresh read SAS URLs for existing blob artifacts using artifact.meta_json.storage_path
        or by parsing artifact.url.
    """

    def __init__(self) -> None:
        self._sas_cfg: Optional[SasConfig] = None
        self._bsc: Optional[BlobServiceClient] = None

    def _get_blob_service(self) -> BlobServiceClient:
        if self._bsc:
            return self._bsc
        conn_str = getattr(settings, "AZURE_STORAGE_CONNECTION_STRING", None)
        if not conn_str:
            raise RuntimeError("azure_storage_not_configured: AZURE_STORAGE_CONNECTION_STRING is not set")
        self._bsc = BlobServiceClient.from_connection_string(conn_str)
        return self._bsc

    def _get_sas_cfg(self) -> SasConfig:
        if self._sas_cfg:
            return self._sas_cfg

        bsc = self._get_blob_service()
        cred = getattr(bsc, "credential", None)

        account_name = getattr(cred, "account_name", None)
        account_key = getattr(cred, "account_key", None)

        if not account_name or not account_key:
            raise RuntimeError(
                "azure_storage_sas_not_configured: unable to derive account_name/account_key from "
                "DF_AZURE_STORAGE_CONNECTION_STRING"
            )

        self._sas_cfg = SasConfig(account_name=str(account_name), account_key=str(account_key))
        return self._sas_cfg

    # -----------------------------
    # Video artifact persistence
    # -----------------------------
    async def persist_video_artifact(
        self,
        provider_video_url: str,
        *,
        user_id: Optional[str] = None,
        job_id: Optional[str] = None,
        provider_job_id: Optional[str] = None,
        ttl_hours: Optional[int] = None,
    ) -> str:
        """
        Download provider video (HeyGen mp4 URL) and persist into Azure Blob.
        Returns an Azure Blob READ SAS URL.

        Container:
          - settings.AZURE_VIDEO_OUTPUT_CONTAINER if set
          - else defaults to "video-output"

        Blob path:
          - if user_id + job_id are provided:
              <user_id>/<job_id>/<provider_job_id or uuid>.mp4
          - else:
              misc/<provider_job_id or uuid>.mp4

        NOTE:
          - Uses sync BlobServiceClient under the hood; upload runs in a thread to avoid blocking event loop.
        """
        url = (provider_video_url or "").strip()
        if not url:
            raise ValueError("provider_video_url is empty")

        container = getattr(settings, "AZURE_VIDEO_OUTPUT_CONTAINER", None) or "video-output"

        # build blob name
        pj = (provider_job_id or "").strip() or uuid.uuid4().hex
        if user_id and job_id:
            blob = f"{str(user_id).strip()}/{str(job_id).strip()}/{pj}.mp4"
        else:
            blob = f"misc/{pj}.mp4"

        # download to temp file (streaming) + compute sha256/bytes (useful later if you want to store)
        tmp_path = None
        size_bytes = 0
        sha256_hex = None

        try:
            fd, tmp_path = tempfile.mkstemp(prefix="df_video_", suffix=".mp4")
            os.close(fd)

            h = hashlib.sha256()

            timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            h.update(chunk)
                            size_bytes += len(chunk)

            if size_bytes <= 0:
                raise ValueError("downloaded_video_is_empty")

            sha256_hex = h.hexdigest()

            # upload to blob (run sync SDK call in a thread)
            bsc = self._get_blob_service()
            blob_client = bsc.get_blob_client(container=container, blob=blob)

            def _upload_sync() -> None:
                with open(tmp_path, "rb") as f:
                    blob_client.upload_blob(
                        f,
                        overwrite=True,
                        content_settings=ContentSettings(content_type="video/mp4"),
                    )

            await asyncio.to_thread(_upload_sync)

            # mint SAS
            cfg = self._get_sas_cfg()
            ttl = int(ttl_hours or getattr(settings, "AZURE_SAS_EXPIRY_HOURS", 2))
            expiry = datetime.now(timezone.utc) + timedelta(hours=ttl)

            sas = generate_blob_sas(
                account_name=cfg.account_name,
                container_name=container,
                blob_name=blob,
                account_key=cfg.account_key,
                permission=BlobSasPermissions(read=True),
                expiry=expiry,
            )

            # SAS URL to persisted Azure video
            return f"https://{cfg.account_name}.blob.core.windows.net/{container}/{blob}?{sas}"

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


    # -----------------------------
    # Azure Blob SAS minting
    # -----------------------------
    async def mint_read_sas_for_artifact(
        self,
        artifact_row: Dict[str, Any],
        ttl_hours: Optional[int] = None,
    ) -> str:
        """
        Mint a fresh read SAS URL for an artifact that lives in Azure Blob.

        Priority:
          1) meta_json.storage_path (preferred)
          2) artifact.url (fallback)

        Handles legacy/buggy storage_path missing container:
          storage_path like "<user_uuid>/<job_uuid>/file.mp3"
          -> container derived from artifact.url, blob derived from storage_path
        """
        meta = _json_to_dict(artifact_row.get("meta_json"))
        storage_path = meta.get("storage_path")

        # Parse URL once (may be needed for fallback / legacy fixups)
        url = str(artifact_row.get("url") or "").strip()
        url_container: Optional[str] = None
        url_blob: Optional[str] = None
        if url:
            try:
                url_container, url_blob = _parse_container_blob_from_url(url)
            except Exception:
                url_container, url_blob = None, None

        container: Optional[str] = None
        blob: Optional[str] = None

        # 1) Try storage_path first
        if isinstance(storage_path, str) and storage_path.strip():
            try:
                c, b = _parse_storage_path(storage_path)

                # If "container" looks like UUID, it's probably actually blob prefix.
                # Use container from URL, and blob = "<uuid>/<rest>"
                if _looks_like_uuid(c):
                    if not url_container:
                        raise ValueError(f"storage_path_missing_container_and_url_unparseable: {storage_path}")
                    container = url_container
                    blob = f"{c}/{b}"
                else:
                    container, blob = c, b

            except Exception:
                container, blob = None, None

        # 2) Fallback: parse from URL
        if not container or not blob:
            if not url_container or not url_blob:
                raise ValueError("artifact_missing_url_and_storage_path")
            container, blob = url_container, url_blob

        cfg = self._get_sas_cfg()
        ttl = int(ttl_hours or getattr(settings, "AZURE_SAS_EXPIRY_HOURS", 2))
        expiry = datetime.now(timezone.utc) + timedelta(hours=ttl)

        sas = generate_blob_sas(
            account_name=cfg.account_name,
            container_name=container,
            blob_name=blob,
            account_key=cfg.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )

        return f"https://{cfg.account_name}.blob.core.windows.net/{container}/{blob}?{sas}"