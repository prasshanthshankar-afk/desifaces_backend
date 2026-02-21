from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import os

from azure.storage.blob import (
    BlobServiceClient,
    BlobSasPermissions,
    ContentSettings,
    generate_blob_sas,
)

from app.config import settings


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
        if se.endswith("Z"):
            se = se[:-1] + "+00:00"

        dt = datetime.fromisoformat(se)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _guess_content_type(path: str) -> str:
    p = (path or "").lower()
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    if p.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def _first_nonempty(*vals: Any) -> Optional[str]:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


@dataclass
class AzureStorageConfig:
    connection_string: str
    container: str
    default_sas_hours: int = 24

    @staticmethod
    def from_env_and_settings() -> "AzureStorageConfig":
        # connection string
        conn = _first_nonempty(
            getattr(settings, "AZURE_STORAGE_CONNECTION_STRING", None),
            os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
            os.getenv("COMMERCE_AZURE_STORAGE_CONNECTION_STRING"),
            os.getenv("DF_AZURE_STORAGE_CONNECTION_STRING"),
        )
        if not conn:
            raise RuntimeError(
                "AzureStorageService: missing AZURE_STORAGE_CONNECTION_STRING "
                "(set in app.config settings or env AZURE_STORAGE_CONNECTION_STRING)"
            )

        # container name (try commerce-first, then common fallbacks)
        container = _first_nonempty(
            getattr(settings, "COMMERCE_OUTPUT_CONTAINER", None),
            getattr(settings, "COMMERCE_CONTAINER", None),
            getattr(settings, "VTON_OUTPUT_CONTAINER", None),
            getattr(settings, "OUTPUT_CONTAINER", None),
            os.getenv("COMMERCE_OUTPUT_CONTAINER"),
            os.getenv("DF_COMMERCE_OUTPUT_CONTAINER"),
            os.getenv("AZURE_STORAGE_CONTAINER"),
        )
        if not container:
            raise RuntimeError(
                "AzureStorageService: missing output container. "
                "Set settings.COMMERCE_OUTPUT_CONTAINER or env COMMERCE_OUTPUT_CONTAINER (or DF_COMMERCE_OUTPUT_CONTAINER)."
            )

        hours = 24
        try:
            hours = int(float(os.getenv("COMMERCE_SAS_HOURS") or "24"))
        except Exception:
            hours = 24

        return AzureStorageConfig(connection_string=conn, container=container, default_sas_hours=hours)


class AzureStorageService:
    """
    svc-commerce Azure Blob helper.

    IMPORTANT:
      - Provides *sync* upload_file/upload_path methods because SareeDrapeProvider calls them synchronously.
      - Also provides get_readonly_sas_url() for API responses if you store raw blob refs.
    """

    def __init__(self, *, config: Optional[AzureStorageConfig] = None):
        self.cfg = config or AzureStorageConfig.from_env_and_settings()
        self.connection_string = self.cfg.connection_string
        self.container = self.cfg.container
        self.blob_service = BlobServiceClient.from_connection_string(self.connection_string)

    # -----------------------------
    # Upload (sync) – used by providers
    # -----------------------------

    def upload_file(
        self,
        local_path: str,
        blob_name: str,
        *,
        content_type: Optional[str] = None,
        overwrite: bool = True,
        sas_hours: Optional[int] = None,
        container_name: Optional[str] = None,
    ) -> str:
        ct = content_type or _guess_content_type(local_path)
        container = container_name or self.container
        blob = (blob_name or "").lstrip("/")

        with open(local_path, "rb") as f:
            data = f.read()

        return self.upload_bytes(
            data=data,
            blob_name=blob,
            content_type=ct,
            overwrite=overwrite,
            sas_hours=sas_hours,
            container_name=container,
        )

    # alias some codebases expect
    upload_path = upload_file

    def upload_local_file(
        self,
        local_path: str,
        blob_name: str,
        *,
        content_type: Optional[str] = None,
        overwrite: bool = True,
        sas_hours: Optional[int] = None,
        container_name: Optional[str] = None,
    ) -> str:
        return self.upload_file(
            local_path,
            blob_name,
            content_type=content_type,
            overwrite=overwrite,
            sas_hours=sas_hours,
            container_name=container_name,
        )

    def upload_bytes(
        self,
        *,
        data: bytes,
        blob_name: str,
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
        sas_hours: Optional[int] = None,
        container_name: Optional[str] = None,
    ) -> str:
        container = container_name or self.container
        blob = (blob_name or "").lstrip("/")

        blob_client = self.blob_service.get_blob_client(container=container, blob=blob)
        blob_client.upload_blob(
            data,
            overwrite=overwrite,
            content_settings=ContentSettings(content_type=content_type),
        )
        return self._generate_sas_url(blob_name=blob, hours=sas_hours or self.cfg.default_sas_hours, container_name=container)

    # -----------------------------
    # SAS helpers
    # -----------------------------

    def _generate_sas_url(self, *, blob_name: str, hours: int = 24, container_name: Optional[str] = None) -> str:
        container = container_name or self.container

        conn_parts = dict(item.split("=", 1) for item in self.connection_string.split(";") if "=" in item)
        account_name = conn_parts.get("AccountName")
        account_key = conn_parts.get("AccountKey")
        if not account_name or not account_key:
            raise RuntimeError("AzureStorageService: could not parse AccountName/AccountKey from connection string")

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
        s = (storage_path_or_url or "").strip()
        mj = meta_json or {}

        # URL
        if s.startswith("http://") or s.startswith("https://"):
            got = _try_parse_container_blob_from_url(s)
            if got:
                return got

        # meta_json explicit
        sc = mj.get("storage_container")
        bn = mj.get("blob_name")
        sp = mj.get("storage_path")

        if isinstance(sc, str) and sc.strip():
            if isinstance(bn, str) and bn.strip():
                return sc.strip(), bn.strip().lstrip("/")
            if isinstance(sp, str) and sp.strip():
                sp2 = _strip_query(sp.strip()).lstrip("/")
                if "/" in sp2 and not sp2.startswith("http"):
                    c, b = sp2.split("/", 1)
                    if c and b:
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

    async def get_readonly_sas_url(
        self,
        *,
        storage_ref: Optional[str],
        meta_json: Optional[Mapping[str, Any]] = None,
        hours: int = 24,
        refresh_if_within_minutes: int = 60,
    ) -> Optional[str]:
        if not storage_ref and not meta_json:
            return None

        now = datetime.utcnow()

        if storage_ref and "?" in storage_ref:
            exp = _try_parse_sas_expiry_utc_naive(storage_ref)
            if exp and exp > (now + timedelta(minutes=refresh_if_within_minutes)):
                return storage_ref
            container, blob_name = self._resolve_container_and_blob_name(storage_path_or_url=storage_ref, meta_json=meta_json)
            return self._generate_sas_url(blob_name=blob_name, hours=hours, container_name=container)

        if storage_ref:
            container, blob_name = self._resolve_container_and_blob_name(storage_path_or_url=storage_ref, meta_json=meta_json)
            return self._generate_sas_url(blob_name=blob_name, hours=hours, container_name=container)

        container, blob_name = self._resolve_container_and_blob_name(storage_path_or_url="", meta_json=meta_json)
        return self._generate_sas_url(blob_name=blob_name, hours=hours, container_name=container)