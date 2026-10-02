#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
face = (root / "services/svc-face/app/app/services/creator_orchestrator.py").read_text()
dashboard = (root / "services/svc-dashboard/app/app/services/dashboard_service.py").read_text()

for marker in (
    "def _group_asset_metadata",
    '"asset_class": "group_photo"',
    '"participant_count": participant_count',
    "**group_asset_metadata",
):
    assert marker in face, marker

for marker in (
    'meta.get("asset_class")',
    'meta.get("participant_count")',
    '"asset_class": asset_class or None',
    '"participant_count": int(participant_count) if participant_count else None',
):
    assert marker in dashboard, marker

# Classification is metadata-only. It must not alter pricing or billing.
for forbidden in (
    "UPDATE pricing_",
    "INSERT INTO pricing_",
    "stripe_price_id",
):
    assert forbidden not in face
    assert forbidden not in dashboard

print("GROUP_PHOTO_LIBRARY_METADATA_SOURCE_TEST=PASS")
