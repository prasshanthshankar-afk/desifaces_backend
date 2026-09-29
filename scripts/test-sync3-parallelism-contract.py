from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
adapter = (ROOT / "services/svc-fusion/app/app/services/providers/sync3_adapter.py").read_text(encoding="utf-8")
script = (ROOT / "scripts/ops/deploy-dev-sync3-parallelism.sh").read_text(encoding="utf-8")

assert "DF_SYNC3_PROVIDER_CONCURRENCY: ${DF_SYNC3_PROVIDER_CONCURRENCY:-3}" in compose
assert "asyncio.Semaphore(limit)" in adapter
assert "last_active < int(self.provider_concurrency)" in adapter
assert "concurrency_limit_reached" in adapter
assert "DEV host desifaces-dev required" in script
assert "active_core_fusion_jobs=" in script
assert "--force-recreate svc-fusion-worker" in script
assert "production=UNTOUCHED" in script

print("SYNC3_PARALLELISM_CONTRACT=PASS")
