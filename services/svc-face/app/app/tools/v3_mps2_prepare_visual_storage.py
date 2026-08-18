from __future__ import annotations

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

from app.config import settings


def _require_v3_container(name: str, *, label: str) -> str:
    value = str(name or "").strip().lower()
    if not value:
        raise RuntimeError(f"MPS2_STORAGE_PREP_FAIL={label}_container_missing")
    if not value.endswith("-v3"):
        raise RuntimeError(
            f"MPS2_STORAGE_PREP_FAIL={label}_container_not_v3_isolated:{value}"
        )
    return value


def _ensure_container(service: BlobServiceClient, *, label: str, name: str) -> None:
    container = service.get_container_client(name)
    if container.exists():
        print(f"MPS2_STORAGE_CONTAINER=PASS:{label}:{name}:existing")
        return
    try:
        container.create_container()
        print(f"MPS2_STORAGE_CONTAINER=PASS:{label}:{name}:created")
    except ResourceExistsError:
        # Another process can create it between exists() and create_container().
        print(f"MPS2_STORAGE_CONTAINER=PASS:{label}:{name}:existing")


def main() -> None:
    output_name = _require_v3_container(settings.FACE_OUTPUT_CONTAINER, label="face_output")
    input_name = _require_v3_container(settings.FACE_INPUT_CONTAINER, label="face_input")

    service = BlobServiceClient.from_connection_string(
        settings.AZURE_STORAGE_CONNECTION_STRING
    )
    _ensure_container(service, label="face_output", name=output_name)
    _ensure_container(service, label="face_input", name=input_name)
    print("MPS2_VISUAL_STORAGE_PRECHECK=PASS")


if __name__ == "__main__":
    main()
