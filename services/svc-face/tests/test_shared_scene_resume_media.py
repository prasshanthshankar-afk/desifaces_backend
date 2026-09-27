from app.api.routes.face_jobs import _asset_blob_location


def test_asset_blob_location_from_explicit_meta():
    container, blob = _asset_blob_location(
        "https://example.blob.core.windows.net/face-output/a/b.png",
        {"storage_container": "face-output", "blob_name": "a/b.png"},
    )
    assert container == "face-output"
    assert blob == "a/b.png"


def test_asset_blob_location_from_azure_storage_ref():
    container, blob = _asset_blob_location(
        "azure://face-output/shared/group-photo.png",
        {},
    )
    assert container == "face-output"
    assert blob == "shared/group-photo.png"


def test_asset_blob_location_from_https_storage_ref():
    container, blob = _asset_blob_location(
        "https://account.blob.core.windows.net/face-output/shared/group-photo.png?sig=old",
        {},
    )
    assert container == "face-output"
    assert blob == "shared/group-photo.png"
