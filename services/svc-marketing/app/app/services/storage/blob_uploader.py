from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from azure.storage.blob import BlobServiceClient, ContentSettings

from app.config import settings


@dataclass
class UploadResult:
    url: str
    container: str
    blob_name: str


class BlobUploader:
    def __init__(self) -> None:
        self.conn_str = settings.AZURE_STORAGE_CONNECTION_STRING
        self.container = settings.AZURE_OUTPUT_CONTAINER
        self.prefix = settings.AZURE_BLOB_PREFIX

    def enabled(self) -> bool:
        return bool(self.conn_str)

    def upload_file(self, local_path: str, blob_name: str, content_type: str) -> UploadResult:
        if not self.enabled():
            return UploadResult(url=f"file://{local_path}", container="local", blob_name=blob_name)

        svc = BlobServiceClient.from_connection_string(self.conn_str)
        cc = svc.get_container_client(self.container)
        try:
            cc.create_container()
        except Exception:
            pass

        full_blob = f"{self.prefix.strip('/')}/{blob_name.lstrip('/')}"
        bc = cc.get_blob_client(full_blob)
        with open(local_path, "rb") as f:
            bc.upload_blob(f, overwrite=True, content_settings=ContentSettings(content_type=content_type))
        return UploadResult(url=bc.url, container=self.container, blob_name=full_blob)