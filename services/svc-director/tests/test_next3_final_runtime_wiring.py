from __future__ import annotations

from pathlib import Path


def test_studio_route_uses_resilient_parallel_parent_priced_executor():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "app"
        / "studio_e2e_routes.py"
    ).read_text(encoding="utf-8")

    assert "ParallelOrphanReconciledParentPricedSceneFusionExecutionService" in source
    assert "fusion_execution = ParallelOrphanReconciledParentPricedSceneFusionExecutionService(" in source
    assert "fusion_execution = SceneFusionExecutionService(" not in source


def test_v3_runtime_serializes_only_sync3_provider_submission():
    compose = Path(__file__).resolve().parents[3] / "docker-compose.v3.yml"
    source = compose.read_text(encoding="utf-8")

    assert "DF_SYNC3_PROVIDER_CONCURRENCY: ${DF_SYNC3_PROVIDER_CONCURRENCY:-1}" in source
    assert "DF_SYNC3_CONCURRENCY_WAIT_SECONDS: ${DF_SYNC3_CONCURRENCY_WAIT_SECONDS:-900}" in source
    assert "svc-director-worker:" in source
    assert "restart: unless-stopped" in source
