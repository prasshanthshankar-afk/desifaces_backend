#!/usr/bin/env python3
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(old, new, 1)


# The segment worker should finish segment work and mark the parent ready for
# stitching, but it must not perform the final compose inline. That prevents a
# slow final compose from occupying a segment-render worker.
worker_path = Path("services/svc-fusion-extension/app/app/workers/longform_worker.py")
worker = worker_path.read_text()
if "DEDICATED_STITCH_HANDOFF_V1" not in worker:
    old = '''    latest_job = await jobs_repo.get_job(conn, longform_job_id)\n    if latest_job:\n        await stitch_if_ready(jobs_repo, segs_repo, conn, dict(latest_job))\n'''
    new = '''    # DEDICATED_STITCH_HANDOFF_V1: when completed_segments reaches total_segments\n    # the SQL above sets parent status='stitching'. The dedicated stitch worker\n    # owns canonical finalization so segment workers immediately return to rendering.\n'''
    worker = replace_once(worker, old, new, "remove inline stitch")
worker_path.write_text(worker)


# The canonical stitch function remains authoritative for final composition and
# parent pricing commit. It must accept the lease state used by the dedicated
# stitch worker and must not downgrade that lease to a claimable state while it
# is actively composing.
orch_path = Path("services/svc-fusion-extension/app/app/services/longform_orchestrator.py")
orch = orch_path.read_text()
if "DEDICATED_STITCH_LEASE_V1" not in orch:
    old_guard = '''    if job_status not in {LongformJobStatus.running.value, LongformJobStatus.stitching.value}:\n        return\n'''
    new_guard = '''    # DEDICATED_STITCH_LEASE_V1: stitching_running is an internal lease\n    # owned by the dedicated stitch worker. It prevents duplicate finalization.\n    if job_status not in {LongformJobStatus.running.value, LongformJobStatus.stitching.value, "stitching_running"}:\n        return\n'''
    orch = replace_once(orch, old_guard, new_guard, "stitch lease guard")

    old_status = '''    await jobs.set_status(conn, job_id, LongformJobStatus.stitching.value)\n\n    rows = await segs.list_by_job(conn, job_id)\n'''
    new_status = '''    # Preserve an active dedicated-worker lease. Direct/legacy callers that\n    # arrive from running still transition to the historical stitching state.\n    if job_status != "stitching_running":\n        await jobs.set_status(conn, job_id, LongformJobStatus.stitching.value)\n\n    rows = await segs.list_by_job(conn, job_id)\n'''
    orch = replace_once(orch, old_status, new_status, "preserve stitch lease")
orch_path.write_text(orch)

print("DEDICATED_STITCH_HANDOFF_PATCH=PASS")
