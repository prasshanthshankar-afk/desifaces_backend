from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from desifaces_shared.v3.studio_workflow_store import (
    CanonicalStudioWorkflowStore,
    StageDependencyNotApproved,
)


class _Conn:
    def __init__(self, blockers):
        self.blockers = blockers
        self.sql = ""
        self.stage_run_id = None

    async def fetch(self, sql, stage_run_id):
        self.sql = sql
        self.stage_run_id = stage_run_id
        return self.blockers


@pytest.mark.asyncio
async def test_dependency_gate_allows_only_approved_usable_parent_outputs():
    stage_run_id = uuid4()
    conn = _Conn([])

    await CanonicalStudioWorkflowStore().assert_startable(conn, stage_run_id=stage_run_id)

    sql = " ".join(conn.sql.split()).lower()
    assert conn.stage_run_id == stage_run_id
    assert "p.state<>'approved'" in sql
    assert "v3_studio_stage_outputs" in sql
    assert "o.is_active=true" in sql
    assert "v3_studio_review_items" in sql
    assert "r.decision='approved'" in sql


@pytest.mark.asyncio
async def test_dependency_gate_fails_closed_for_any_unusable_parent():
    parent_id = uuid4()
    child_id = uuid4()
    conn = _Conn([
        {
            "stage_run_id": parent_id,
            "stage_type": "audio",
            "state": "approved",
        }
    ])

    with pytest.raises(StageDependencyNotApproved) as exc:
        await CanonicalStudioWorkflowStore().assert_startable(conn, stage_run_id=child_id)

    message = str(exc.value)
    assert "stage_dependencies_not_approved_or_usable" in message
    assert str(child_id) in message
    assert "audio" in message
    assert str(parent_id) in message


def test_story_workflow_declares_face_audio_fusion_story_final_dependencies():
    source = Path(__file__).resolve().parents[1] / "app" / "app" / "studio_workflow.py"
    text = source.read_text(encoding="utf-8")

    # Audio is blocked by the complete Face cohort.
    assert "for face_stage_id in required_face_stages" in text
    assert "child_stage_run_id=stage_id" in text

    # Each Scene/Fusion stage depends on its Face members and dialogue Audio stages.
    assert "for participant_id in members_by_scene.get(scene.scene_id, ())" in text
    assert "child_stage_run_id=fusion_stage" in text
    assert "for audio_stage in audio_by_scene.get(scene.scene_id, ())" in text
    assert "parent_stage_run_id=audio_stage" in text

    # Multi-scene Story Final is blocked by every Scene/Fusion stage.
    assert "for fusion_stage in fusion_by_scene.values()" in text
    assert "parent_stage_run_id=fusion_stage, child_stage_run_id=final_stage" in text
