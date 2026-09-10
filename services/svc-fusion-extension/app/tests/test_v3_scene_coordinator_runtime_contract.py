from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WORKER = ROOT / "services/svc-fusion-extension/app/app/workers/stitch_worker.py"
COORDINATOR = ROOT / "services/svc-fusion-extension/app/app/workers/v3_scene_coordinator.py"
COMPOSE_V3 = ROOT / "docker-compose.v3.yml"


def test_stitch_worker_runs_v3_scene_coordinator() -> None:
    text = WORKER.read_text(encoding="utf-8")
    assert "from app.workers.v3_scene_coordinator import v3_scene_coordinator_loop" in text
    assert "v3_scene_coordinator_loop()" in text
    assert "asyncio.gather(" in text


def test_coordinator_claims_only_dispatch_complete_parent_reserved_scenes() -> None:
    text = COORDINATOR.read_text(encoding="utf-8")
    assert "s.state='generating'" in text
    assert "a.state in ('running','succeeded')" in text
    assert "{fusion_parent_pricing,state}" in text
    assert "('reserved','commit_pending')" in text
    assert "background_coordinator" in text
    assert 'phase="scene_stitch"' in text
    assert "commit_scene_pricing" in text


def test_dev_v3_compose_enables_server_side_scene_coordinator() -> None:
    text = COMPOSE_V3.read_text(encoding="utf-8")
    service = text.split("svc-fusion-extension-stitch-worker:", 1)[1].split("\n  svc-music-worker:", 1)[0]
    assert 'DF_V3_SCENE_COORDINATOR_ENABLED: "true"' in service
    assert 'DF_V3_SCENE_COORDINATOR_POLL_SECONDS: "2"' in service
    assert "SVC_FUSION_BASE_URL:" in service
