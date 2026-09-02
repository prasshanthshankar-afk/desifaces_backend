from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
route = (ROOT / "app/app/api/routes/longform.py").read_text()
repo = (ROOT / "app/app/repos/longform_jobs_repo.py").read_text()

start = route.index('async def create_longform_job(')
end = route.index('@router.get("/jobs/{job_id}"', start)
create = route[start:end]

assert 'initial_status="pricing_pending"' in create
assert "reserve_longform_pricing_for_job" in create
assert "await segs_repo.insert_segment" in create
assert "SET status = 'queued'" in create

p_pending = create.index('initial_status="pricing_pending"')
p_reserve = create.index('reserve_longform_pricing_for_job')
p_insert = create.index('await segs_repo.insert_segment')
p_activate = create.index("SET status = 'queued'")
assert p_pending < p_reserve < p_insert < p_activate, (p_pending, p_reserve, p_insert, p_activate)

# Failure handling must occur before any segment materialization.
p_block = create.index('failed_status = "blocked"')
assert p_reserve < p_block < p_insert

assert 'initial_status: str = "queued"' in repo
assert '$13::text' in repo
assert 'str(initial_status or "queued")' in repo

print("LONGFORM_PRICING_GATE_ORDER_TEST=PASS")
