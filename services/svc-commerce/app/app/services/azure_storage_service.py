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

# Be robust if app/app/config.py is empty or settings lacks fields
try:
    from app.config import settings  # type: ignore
except Exception:  # pragma: no cover
    class _SettingsFallback:  # minimal fallback
        AZURE_STORAGE_CONNECTION_STRING = ""
        COMMERCE_OUTPUT_CONTAINER = ""
    settings = _SettingsFallback()  # type: ignore


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
        # normalize to naive UTC
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
    if p.endswith(".blend"):
        return "application/octet-stream"
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
                "(set in env AZURE_STORAGE_CONNECTION_STRING)"
            )

        # container name (commerce-first)
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
                "Set env COMMERCE_OUTPUT_CONTAINER (or DF_COMMERCE_OUTPUT_CONTAINER)."
            )

        try:
            hours = int(float(os.getenv("COMMERCE_SAS_HOURS") or "24"))
        except Exception:
            hours = 24

        return AzureStorageConfig(connection_string=conn, container=container, default_sas_hours=hours)


class AzureStorageService:
    """
    svc-commerce Azure Blob helper.

    Key goals:
      - Sync upload_file/upload_path/upload (SareeDrapeProvider calls sync)
      - SAS URL generation + refresh helpers
      - NEW: Sync get_blob_sas_url(container, blob_name, expires_in_s) for Option-B (az:// refs)
    """

    def __init__(self, *, config: Optional[AzureStorageConfig] = None):
        self.cfg = config or AzureStorageConfig.from_env_and_settings()
        self.connection_string = self.cfg.connection_string
        self.container = self.cfg.container
        self.blob_service = BlobServiceClient.from_connection_string(self.connection_string)

        # parse account name/key once
        conn_parts = dict(item.split("=", 1) for item in self.connection_string.split(";") if "=" in item)
        self._account_name = conn_parts.get("AccountName")
        self._account_key = conn_parts.get("AccountKey")
        if not self._account_name or not self._account_key:
            raise RuntimeError("AzureStorageService: could not parse AccountName/AccountKey from connection string")

    # -----------------------------
    # Upload (sync)
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

    # Common aliases expected by different code paths
    upload_path = upload_file
    upload_local_file = upload_file
    upload = upload_file  # IMPORTANT: some callers look for `upload(...)`

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
        return self._generate_sas_url(
            blob_name=blob,
            hours=sas_hours or self.cfg.default_sas_hours,
            container_name=container,
        )

    # -----------------------------
    # SAS helpers (hours-based, existing)
    # -----------------------------

    def _generate_sas_url(self, *, blob_name: str, hours: int = 24, container_name: Optional[str] = None) -> str:
        container = container_name or self.container

        sas_token = generate_blob_sas(
            account_name=self._account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=self._account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=hours),
        )
        return f"https://{self._account_name}.blob.core.windows.net/{container}/{blob_name}?{sas_token}"

    # -----------------------------
    # SAS helpers (seconds-based, NEW)
    # -----------------------------

    def _generate_sas_url_seconds(
        self,
        *,
        container_name: str,
        blob_name: str,
        expires_in_s: int,
        permission: str = "r",
    ) -> str:
        """
        permission:
          - "r"  read
          - "w"  write (rarely needed)
          - "rw" read+write
        """
        perm = (permission or "r").lower().strip()

        perms = BlobSasPermissions(read=("r" in perm))
        # only enable write if explicitly requested
        if "w" in perm:
            perms.write = True
            perms.create = True
            perms.add = True

        expiry = datetime.utcnow() + timedelta(seconds=int(expires_in_s))
        sas_token = generate_blob_sas(
            account_name=self._account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=self._account_key,
            permission=perms,
            expiry=expiry,
        )
        return f"https://{self._account_name}.blob.core.windows.net/{container_name}/{blob_name}?{sas_token}"

    def get_blob_sas_url(
        self,
        *,
        container: str,
        blob_name: str,
        expires_in_s: int = 3600,
        permission: str = "r",
    ) -> str:
        """
        Sync: returns a SAS URL for any container/blob in the same storage account.

        This is the method SareeDrapeProvider Option-B should call to sign:
          DF_SAREE_TEMPLATE_NIVI_REF="az://commerce-training/drape_templates/.../nivi.blend"
        """
        c = (container or "").strip()
        b = (blob_name or "").strip().lstrip("/")
        if not c or not b:
            raise ValueError(f"get_blob_sas_url requires container and blob_name (got container={container!r} blob={blob_name!r})")
        return self._generate_sas_url_seconds(container_name=c, blob_name=b, expires_in_s=int(expires_in_s), permission=permission)

    # Aliases (so callers don't have to match one exact name)
    get_sas_url = get_blob_sas_url
    create_sas_url = get_blob_sas_url
    generate_sas_url = get_blob_sas_url
    get_signed_url = get_blob_sas_url
    sign_url = get_blob_sas_url
    get_read_url = get_blob_sas_url
    get_signed_read_url = get_blob_sas_url
    refresh_sas_url = get_blob_sas_url

    # -----------------------------
    # Resolve helpers (existing)
    # -----------------------------

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

    async def regenerate_sas_url(self, storage_path_or_url: str, *, hours: int = 24) -> str:
        """
        Accepts:
          - blob_name ("commerce/vton/.../x.png")
          - "container/blob"
          - full URL with or without SAS
        """
        container, blob_name = self._resolve_container_and_blob_name(storage_path_or_url=storage_path_or_url)
        return self._generate_sas_url(blob_name=blob_name, hours=hours, container_name=container)

    async def get_readonly_sas_url(
        self,
        *,
        storage_ref: Optional[str],
        meta_json: Optional[Mapping[str, Any]] = None,
        hours: int = 24,
        refresh_if_within_minutes: int = 60,
    ) -> Optional[str]:
        """
        If storage_ref already has SAS and isn't expiring soon -> return it.
        Else regenerate a SAS URL.
        """
        if not storage_ref and not meta_json:
            return None

        now = datetime.utcnow()

        if storage_ref and "?" in storage_ref:
            exp = _try_parse_sas_expiry_utc_naive(storage_ref)
            if exp and exp > (now + timedelta(minutes=refresh_if_within_minutes)):
                return storage_ref
            container, blob_name = self._resolve_container_and_blob_name(
                storage_path_or_url=storage_ref,
                meta_json=meta_json,
            )
            return self._generate_sas_url(blob_name=blob_name, hours=hours, container_name=container)

        if storage_ref:
            container, blob_name = self._resolve_container_and_blob_name(
                storage_path_or_url=storage_ref,
                meta_json=meta_json,
            )
            return self._generate_sas_url(blob_name=blob_name, hours=hours, container_name=container)

        container, blob_name = self._resolve_container_and_blob_name(storage_path_or_url="", meta_json=meta_json)
        return self._generate_sas_url(blob_name=blob_name, hours=hours, container_name=container)