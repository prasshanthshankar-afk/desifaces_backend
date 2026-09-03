#!/usr/bin/env python3
from pathlib import Path

path = Path("services/svc-fusion-extension/app/app/workers/longform_worker.py")
text = path.read_text()

if "def _effective_max_inflight_per_job" not in text:
    anchor = '''def _segment_worker_concurrency() -> int:\n    configured = (\n'''
    helper = '''def _effective_max_inflight_per_job() -> int:\n    """Launch performance floor for independent segment fan-out.\n\n    Older deployments may still carry MAX_INFLIGHT_SEGMENTS_PER_JOB=2 in the\n    environment. Preserve higher operator settings, but do not allow that legacy\n    default to serialize a typical 60-120 second parent into waves of two.\n    """\n    try:\n        configured = max(1, int(settings.MAX_INFLIGHT_SEGMENTS_PER_JOB))\n    except Exception:\n        configured = 1\n    try:\n        batch = max(1, int(settings.WORKER_BATCH_SIZE))\n    except Exception:\n        batch = 8\n    return min(batch, max(configured, min(8, batch)))\n\n\n'''
    if anchor not in text:
        raise SystemExit("worker parallelism helper anchor missing")
    text = text.replace(anchor, helper + anchor, 1)

    text = text.replace(
        '''        settings.MAX_INFLIGHT_SEGMENTS_PER_JOB,\n    )\n\n    while True:\n''',
        '''        _effective_max_inflight_per_job(),\n    )\n\n    while True:\n''',
        1,
    )
    text = text.replace(
        '''                settings.MAX_INFLIGHT_SEGMENTS_PER_JOB,\n            )\n''',
        '''                _effective_max_inflight_per_job(),\n            )\n''',
        1,
    )

path.write_text(text)
print("V3_VIDEO_WORKER_PARALLELISM_PATCH=PASS")
