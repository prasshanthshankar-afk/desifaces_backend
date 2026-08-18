from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from df_contracts.v3.domain import ParticipantKind
from df_contracts.v3.story import (
    DialogueTurn, DialogueTurnKind, Participant, Project, Scene, SceneParticipant,
    Story, StoryGraph, StoryParticipant,
)
from df_contracts.v3.studio_workflow import (
    StudioScopeType, StudioStageState, StudioStageType, StudioStageView,
)
from desifaces_shared.v3.studio_workflow_store import _updated_rows


def now():
    return datetime.now(timezone.utc)


def graph_2p_2scene() -> StoryGraph:
    account_id, owner_user_id, project_id, story_id = uuid4(), uuid4(), uuid4(), uuid4()
    p1 = Participant(participant_id=uuid4(), account_id=account_id, project_id=project_id,
                     kind=ParticipantKind.PERSON, display_name="Ananya", created_at=now(), updated_at=now())
    p2 = Participant(participant_id=uuid4(), account_id=account_id, project_id=project_id,
                     kind=ParticipantKind.PERSON, display_name="Ravi", created_at=now(), updated_at=now())
    s1, s2 = uuid4(), uuid4()
    scenes = (
        Scene(scene_id=s1, story_id=story_id, sequence=0, title="Scene 1", created_at=now(), updated_at=now()),
        Scene(scene_id=s2, story_id=story_id, sequence=1, title="Scene 2", created_at=now(), updated_at=now()),
    )
    turns = []
    for scene_id in (s1, s2):
        turns.extend([
            DialogueTurn(turn_id=uuid4(), scene_id=scene_id, sequence=0, kind=DialogueTurnKind.SPEECH,
                         speaker_participant_id=p1.participant_id, text="Hello", created_at=now()),
            DialogueTurn(turn_id=uuid4(), scene_id=scene_id, sequence=1, kind=DialogueTurnKind.SPEECH,
                         speaker_participant_id=p2.participant_id, text="Hi", created_at=now()),
        ])
    return StoryGraph(
        project=Project(project_id=project_id, account_id=account_id, owner_user_id=owner_user_id,
                        title="Project", created_at=now(), updated_at=now()),
        participants=(p1, p2),
        story=Story(story_id=story_id, account_id=account_id, project_id=project_id,
                    title="Story", created_at=now(), updated_at=now()),
        story_participants=(
            StoryParticipant(story_id=story_id, participant_id=p1.participant_id, sequence=0),
            StoryParticipant(story_id=story_id, participant_id=p2.participant_id, sequence=1),
        ),
        scenes=scenes,
        scene_participants=tuple(
            SceneParticipant(scene_id=scene.scene_id, participant_id=p.participant_id, sequence=i)
            for scene in scenes for i, p in enumerate((p1, p2))
        ),
        dialogue_turns=tuple(turns),
    )


class RecordingStore:
    def __init__(self):
        self.workflow_id = uuid4()
        self.stages = []
        self.dependencies = []
        self.state = None

    async def create_workflow(self, conn, **kwargs):
        return self.workflow_id

    async def add_stage(self, conn, **kwargs):
        stage_id = uuid4()
        self.stages.append((stage_id, kwargs))
        return stage_id

    async def add_dependency(self, conn, **kwargs):
        self.dependencies.append((kwargs["parent_stage_run_id"], kwargs["child_stage_run_id"]))

    async def set_workflow_state(self, conn, **kwargs):
        self.state = kwargs


def test_story_builds_face_audio_fusion_and_final_hitl_dag():
    from app.studio_workflow import build_story_studio_workflow

    async def run():
        graph = graph_2p_2scene()
        store = RecordingStore()
        workflow_id = await build_story_studio_workflow(
            None, graph=graph, owner_user_id=graph.project.owner_user_id, store=store
        )
        assert workflow_id == store.workflow_id
        by_type = {}
        for stage_id, kwargs in store.stages:
            by_type.setdefault(kwargs["stage_type"], []).append((stage_id, kwargs))
        assert len(by_type[StudioStageType.FACE]) == 2
        assert len(by_type[StudioStageType.AUDIO]) == 4
        assert len(by_type[StudioStageType.FUSION]) == 2
        assert len(by_type[StudioStageType.STORY_FINAL]) == 1
        assert all(kwargs["scope_type"] is StudioScopeType.PARTICIPANT for _, kwargs in by_type[StudioStageType.FACE])
        assert all(kwargs["scope_type"] is StudioScopeType.DIALOGUE_TURN for _, kwargs in by_type[StudioStageType.AUDIO])
        assert all(kwargs["scope_type"] is StudioScopeType.SCENE for _, kwargs in by_type[StudioStageType.FUSION])
        assert by_type[StudioStageType.STORY_FINAL][0][1]["scope_type"] is StudioScopeType.STORY

        parents = {}
        for parent, child in store.dependencies:
            parents.setdefault(child, set()).add(parent)
        face_ids = {x[0] for x in by_type[StudioStageType.FACE]}
        audio_ids = {x[0] for x in by_type[StudioStageType.AUDIO]}
        fusion_ids = {x[0] for x in by_type[StudioStageType.FUSION]}
        assert all(parents[a] <= face_ids and len(parents[a]) == 1 for a in audio_ids)
        assert all((parents[f] & face_ids) and (parents[f] & audio_ids) for f in fusion_ids)
        final_id = by_type[StudioStageType.STORY_FINAL][0][0]
        assert parents[final_id] == fusion_ids
        assert store.state["current_stage"] is StudioStageType.FACE

    asyncio.run(run())


def test_stage_scope_contract_rejects_extraneous_ids():
    common = dict(
        stage_run_id=uuid4(), workflow_id=uuid4(), stage_type=StudioStageType.FACE,
        state=StudioStageState.PENDING, created_at=now(), updated_at=now(),
    )
    with pytest.raises(ValidationError):
        StudioStageView(
            **common, scope_type=StudioScopeType.PARTICIPANT,
            participant_id=uuid4(), scene_id=uuid4(),
        )


def test_asyncpg_update_tag_parsing_is_exact():
    assert _updated_rows("UPDATE 0") == 0
    assert _updated_rows("UPDATE 1") == 1
    assert _updated_rows("UPDATE 10") == 10


def test_director_claim_uses_integer_safe_lease_interval():
    from app.run_store import DirectorRunStore

    class CaptureConn:
        query = None
        args = None

        async def fetchrow(self, query, *args):
            self.query = query
            self.args = args
            return None

    async def run():
        conn = CaptureConn()
        await DirectorRunStore().claim_next(conn, lease_seconds=900)
        assert "make_interval(secs => $1::integer)" in conn.query
        assert conn.args == (900,)

    asyncio.run(run())


def test_shared_studio_artifact_trigger_is_record_shape_safe():
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "2026_08_18_v3_studio_hitl_hardening.sql"
    ).read_text(encoding="utf-8")
    start = migration.index("CREATE OR REPLACE FUNCTION public.df_v3_validate_studio_artifact()")
    end = migration.index("DROP TRIGGER IF EXISTS trg_df_v3_studio_input_artifact", start)
    function_sql = migration[start:end]
    assert "NEW.source_stage_run_id" not in function_sql
    assert "to_jsonb(NEW)->>'source_stage_run_id'" in function_sql
