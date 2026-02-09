from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas


def _parse_conn_str(conn_str: str) -> dict:
    parts = {}
    for kv in conn_str.split(";"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


@dataclass
class AzureBlobService:
    connection_string: str

    def __post_init__(self) -> None:
        self._bsc = BlobServiceClient.from_connection_string(self.connection_string)
        p = _parse_conn_str(self.connection_string)
        self._account_name = p.get("AccountName")
        self._account_key = p.get("AccountKey")

    def upload_file(self, container: str, blob_name: str, local_path: str, content_type: Optional[str] = None) -> None:
        blob_name = blob_name.lstrip("/")
        bc = self._bsc.get_blob_client(container=container, blob=blob_name)
        cs = ContentSettings(content_type=content_type) if content_type else None
        with open(local_path, "rb") as f:
            bc.upload_blob(f, overwrite=True, content_settings=cs)

    def sign_read_url(self, container: str, blob_name: str, ttl_seconds: int) -> str:
        if not self._account_name or not self._account_key:
            raise RuntimeError("Azure connection string missing AccountName/AccountKey required for SAS")
        blob_name = blob_name.lstrip("/")
        expiry = datetime.now(timezone.utc) + timedelta(seconds=int(ttl_seconds))
        sas = generate_blob_sas(
            account_name=self._account_name,
            account_key=self._account_key,
            container_name=container,
            blob_name=blob_name,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        return f"https://{self._account_name}.blob.core.windows.net/{container}/{blob_name}?{sas}"


def parse_blob_path_from_sas_url(url: str) -> tuple[str, str]:
    """
    Returns (container, blob_name) from:
      https://account.blob.core.windows.net/<container>/<blob>?<sas>
    """
    u = urlparse(url)
    path = u.path.lstrip("/")
    container, blob = path.split("/", 1)
    return container, blob