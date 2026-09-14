from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
FACE = ROOT / "services" / "svc-face" / "app" / "app"


def test_face_worker_uses_bounded_job_parallelism():
    text = (FACE / "workers" / "face_worker.py").read_text()
    assert 'DF_FACE_JOB_CONCURRENCY' in text
    assert '"2"' in text
    assert 'limit=capacity' in text
    assert 'asyncio.create_task' in text
    assert 'return_when=asyncio.FIRST_COMPLETED' in text
    assert 'limit=1' not in text


def test_canonical_face_read_url_is_mounted():
    route = (FACE / "api" / "routes" / "canonical_face_assets.py").read_text()
    api = (FACE / "api" / "__init__.py").read_text()
    assert '@router.get("/assets/{media_id}/read-url"' in route
    assert 'get_readonly_sas_url' in route
    assert 'asset_user_id != actor_id' in route
    assert 'canonical_face_assets_router' in api
    assert 'router.include_router(canonical_face_assets_router, prefix="/api/face"' in api
