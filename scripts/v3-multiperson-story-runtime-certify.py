from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg

from df_contracts.v3.common import ActorType, RequestActor, RequestContext
from df_contracts.v3.domain import GenerationKind, GenerationRequest, ParticipantKind, SafetyState
from df_contracts.v3.story import (
    DialogueTurn,
    DialogueTurnKind,
    Participant,
    Project,
    Scene,
    SceneParticipant,
    Story,
    StoryGraph,
    StoryParticipant,
)
from desifaces_shared.v3.story_generation import CanonicalStoryGenerationStore
from desifaces_shared.v3.story_store import CanonicalStoryStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _scalar(conn, sql: str, *args) -> int:
    return int(await conn.fetchval(sql, *args) or 0)


def _build_graph(*, account_id: UUID, user_id: UUID, participant_count: int, label: str) -> StoryGraph:
    project = Project(
        account_id=account_id,
        owner_user_id=user_id,
        title=f"{label} project",
        metadata={"certification": "V3-MULTIPERSON-STORY"},
        created_at=_now(),
        updated_at=_now(),
    )
    participants = tuple(
        Participant(
            account_id=account_id,
            project_id=project.project_id,
            kind=ParticipantKind.PERSON,
            display_name=f"{label} participant {index + 1}",
            persona={"cert_index": index},
            continuity={"identity_locked": True},
            created_at=_now(),
            updated_at=_now(),
        )
        for index in range(participant_count)
    )
    story = Story(
        account_id=account_id,
        project_id=project.project_id,
        title=f"{label} story",
        synopsis="Transactional certification story",
        created_at=_now(),
        updated_at=_now(),
    )
    scene = Scene(
        story_id=story.story_id,
        sequence=0,
        title="Scene 1",
        setting={"location": "certification"},
        direction={"camera": "wide" if participant_count > 1 else "medium"},
        created_at=_now(),
        updated_at=_now(),
    )
    story_members = tuple(
        StoryParticipant(story_id=story.story_id, participant_id=p.participant_id, sequence=index)
        for index, p in enumerate(participants)
    )
    scene_members = tuple(
        SceneParticipant(
            scene_id=scene.scene_id,
            participant_id=p.participant_id,
            sequence=index,
            placement={"slot": index},
        )
        for index, p in enumerate(participants)
    )
    turns = tuple(
        DialogueTurn(
            scene_id=scene.scene_id,
            sequence=index,
            kind=DialogueTurnKind.SPEECH,
            speaker_participant_id=p.participant_id,
            text=f"Certification line {index + 1}",
            locale="en",
            created_at=_now(),
        )
        for index, p in enumerate(participants)
    )
    return StoryGraph(
        project=project,
        participants=participants,
        story=story,
        story_participants=story_members,
        scenes=(scene,),
        scene_participants=scene_members,
        dialogue_turns=turns,
    )


async def main() -> None:
    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        raise SystemExit("MULTIPERSON_CERT_FAIL=DATABASE_URL_missing")

    conn = await asyncpg.connect(database_url)
    try:
        required = [
            "public.v3_projects",
            "public.v3_participants",
            "public.v3_participant_media",
            "public.v3_stories",
            "public.v3_story_participants",
            "public.v3_scenes",
            "public.v3_scene_participants",
            "public.v3_dialogue_turns",
            "public.v3_generation_requests",
            "public.v3_generation_jobs",
        ]
        for name in required:
            if not await conn.fetchval("select to_regclass($1)", name):
                raise RuntimeError(f"MULTIPERSON_CERT_FAIL=required_relation_missing:{name}")
        print("MULTIPERSON_STORY_SCHEMA=PASS")

        actor_row = await conn.fetchrow(
            """
            select bam.user_id,bam.billing_account_id
            from public.pricing_billing_account_members bam
            join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
            where bam.status='active' and ba.status='active'
            order by bam.is_default desc,bam.created_at asc
            limit 1
            """
        )
        if not actor_row:
            raise RuntimeError("MULTIPERSON_CERT_FAIL=no_v3_account_context")
        user_id = UUID(str(actor_row["user_id"]))
        account_id = UUID(str(actor_row["billing_account_id"]))

        tracked_tables = {
            "projects": "public.v3_projects",
            "participants": "public.v3_participants",
            "participant_media": "public.v3_participant_media",
            "stories": "public.v3_stories",
            "story_participants": "public.v3_story_participants",
            "scenes": "public.v3_scenes",
            "scene_participants": "public.v3_scene_participants",
            "dialogue": "public.v3_dialogue_turns",
            "requests": "public.v3_generation_requests",
            "jobs": "public.v3_generation_jobs",
            "events": "public.v3_generation_job_events",
        }
        baseline = {key: await _scalar(conn, f"select count(*) from {table}") for key, table in tracked_tables.items()}

        tx = conn.transaction()
        await tx.start()
        try:
            story_store = CanonicalStoryStore()
            generation_store = CanonicalStoryGenerationStore()

            single = _build_graph(account_id=account_id, user_id=user_id, participant_count=1, label="single")
            single_read = await story_store.create_graph(conn, graph=single)
            if single_read.participant_count != 1 or len(single_read.scenes) != 1 or len(single_read.dialogue_turns) != 1:
                raise RuntimeError("MULTIPERSON_CERT_FAIL=single_person_graph_roundtrip")
            print("ONE_PERSON_STORY_GRAPH=PASS")

            multi = _build_graph(account_id=account_id, user_id=user_id, participant_count=3, label="multi")
            multi_read = await story_store.create_graph(conn, graph=multi)
            if multi_read.participant_count != 3 or len(multi_read.scene_participants) != 3 or len(multi_read.dialogue_turns) != 3:
                raise RuntimeError("MULTIPERSON_CERT_FAIL=multi_person_graph_roundtrip")
            print("MULTI_PERSON_STORY_GRAPH=PASS")

            request = GenerationRequest(
                account_id=account_id,
                requested_by_user_id=user_id,
                project_id=multi.project.project_id,
                story_id=multi.story.story_id,
                scene_id=multi.scenes[0].scene_id,
                kind=GenerationKind.FUSION,
                participant_ids=tuple(p.participant_id for p in multi.participants),
                parameters={"certification": True, "participant_count": 3},
                safety_state=SafetyState.ALLOWED,
                created_at=_now(),
            )
            ctx = RequestContext(
                actor=RequestActor(
                    actor_type=ActorType.USER,
                    actor_id=user_id,
                    account_id=account_id,
                ),
                idempotency_key="v3-multiperson-story-certification",
                client_app="v3-certifier",
            )
            persisted = await generation_store.create_request_and_root_job(
                conn,
                request=request,
                context=ctx,
                idempotency_key="v3-multiperson-story-certification",
                request_digest="v3-multiperson-story-certification-digest",
                metadata={"certification": "V3-MULTIPERSON-STORY"},
            )
            replay = await generation_store.create_request_and_root_job(
                conn,
                request=request,
                context=ctx,
                idempotency_key="v3-multiperson-story-certification",
                request_digest="v3-multiperson-story-certification-digest",
            )
            if replay.created or replay.generation_id != persisted.generation_id or replay.job_id != persisted.job_id:
                raise RuntimeError("MULTIPERSON_CERT_FAIL=story_generation_idempotency")

            row = await conn.fetchrow(
                """
                select story_id,scene_id,participant_ids
                from public.v3_generation_requests where generation_id=$1
                """,
                persisted.generation_id,
            )
            if not row:
                raise RuntimeError("MULTIPERSON_CERT_FAIL=story_generation_missing")
            if UUID(str(row["story_id"])) != multi.story.story_id or UUID(str(row["scene_id"])) != multi.scenes[0].scene_id:
                raise RuntimeError("MULTIPERSON_CERT_FAIL=story_scene_generation_linkage")
            if set(UUID(str(x)) for x in row["participant_ids"]) != {p.participant_id for p in multi.participants}:
                raise RuntimeError("MULTIPERSON_CERT_FAIL=participant_generation_linkage")
            print("STORY_SCENE_GENERATION_LINKAGE=PASS")
            print("STORY_GENERATION_IDEMPOTENCY=PASS")

            outsider = Participant(
                account_id=account_id,
                project_id=multi.project.project_id,
                display_name="Not in story",
                created_at=_now(),
                updated_at=_now(),
            )
            await story_store.create_participant(conn, participant=outsider)
            bad = request.model_copy(update={
                "generation_id": uuid4(),
                "participant_ids": request.participant_ids + (outsider.participant_id,),
            })
            try:
                await generation_store.create_request_and_root_job(
                    conn,
                    request=bad,
                    context=ctx,
                    idempotency_key="v3-multiperson-story-negative-cert",
                    request_digest="v3-multiperson-story-negative-digest",
                )
            except ValueError as exc:
                if "generation_participant_not_in_story" not in str(exc):
                    raise
            else:
                raise RuntimeError("MULTIPERSON_CERT_FAIL=outsider_participant_generation_allowed")
            print("STORY_PARTICIPANT_OWNERSHIP_GUARD=PASS")
        finally:
            await tx.rollback()

        after = {key: await _scalar(conn, f"select count(*) from {table}") for key, table in tracked_tables.items()}
        if after != baseline:
            raise RuntimeError(f"MULTIPERSON_CERT_FAIL=rollback:before={baseline}:after={after}")
        print("MULTIPERSON_STORY_CERTIFICATION_ROLLBACK=PASS")
        print("V3_MULTIPERSON_STORY_FOUNDATION_CERTIFICATION=PASS")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
