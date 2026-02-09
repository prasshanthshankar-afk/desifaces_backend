from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

from azure.storage.blob import generate_blob_sas, BlobSasPermissions


def _parse_conn_str(conn_str: str) -> dict:
    parts = {}
    for kv in (conn_str or "").split(";"):
        if not kv.strip() or "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        parts[k.strip()] = v.strip()
    return parts


def split_container_blob_from_url(url: str) -> Optional[Tuple[str, str]]:
    """
    Extract (container, blob) from:
      https://<account>.blob.core.windows.net/<container>/<blob>?<sas>
    """
    if not url:
        return None
    try:
        u = urlparse(url)
        path = (u.path or "").lstrip("/")
        container, _, blob = path.partition("/")
        if container and blob:
            return container, blob
        return None
    except Exception:
        return None


@dataclass(frozen=True)
class AzureBlobSasSigner:
    account_name: str
    account_key: str

    @classmethod
    def from_connection_string(cls, conn_str: str) -> "AzureBlobSasSigner":
        d = _parse_conn_str(conn_str)
        name = d.get("AccountName")
        key = d.get("AccountKey")
        if not name or not key:
            raise ValueError("AZURE_STORAGE_CONNECTION_STRING must include AccountName and AccountKey")
        return cls(account_name=name, account_key=key)

    def sign_read_url(self, container: str, storage_path: str, ttl_seconds: int) -> str:
        """
        Signs a read-only SAS URL for: https://{account}.blob.core.windows.net/{container}/{storage_path}
        """
        blob_name = (storage_path or "").lstrip("/")
        if not blob_name:
            raise ValueError("storage_path is empty")

        expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        sas = generate_blob_sas(
            account_name=self.account_name,
            account_key=self.account_key,
            container_name=container,
            blob_name=blob_name,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        return f"https://{self.account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"