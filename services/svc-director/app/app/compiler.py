from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from df_contracts.v3.director import CreativeBrief, CreativeStoryPlan
from df_contracts.v3.domain import EntityState
from df_contracts.v3.story import (
    DialogueTurn,
    Participant,
    Project,
    Scene,
    SceneParticipant,
    Story,
    StoryGraph,
    StoryParticipant,
)
from desifaces_shared.v3.story_store import CanonicalStoryStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(thread_id: str, kind: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"desifaces:v3:director:{thread_id}:{kind}:{key}")


class CanonicalStoryCompiler:
    """Compile a validated AI plan into canonical relational Story state.

    IDs are deterministic per Director thread, making replay/retry safe. Existing
    account/project identity comes from retrieval/tool context, never from LLM output.
    A model-supplied participant UUID is accepted only when that exact participant
    already exists in the authenticated account/project; otherwise it is ignored.
    """

    def __init__(self, pool) -> None:
        self._pool = pool
        self._store = CanonicalStoryStore()

    async def compile(
        self,
        *,
        brief: CreativeBrief,
        plan: CreativeStoryPlan,
        retrieved_context: dict[str, Any],
    ) -> StoryGraph:
        structured = dict(retrieved_context.get("structured") or {})
        account_id = UUID(str(structured["account_id"]))
        owner_user_id = UUID(str(structured["owner_user_id"]))
        thread_id = str(retrieved_context.get("thread_id") or structured.get("thread_id") or "").strip()
        if not thread_id:
            raise RuntimeError("director_thread_id_required_for_idempotent_compile")

        project_id = brief.project_id or _id(thread_id, "project", "root")
        story_id = brief.story_id or _id(thread_id, "story", "root")
        now = _now()

        async with self._pool.acquire() as conn:
            existing_story = await conn.fetchval(
                "select story_id from public.v3_stories where story_id=$1 and account_id=$2",
                story_id,
                account_id,
            )
            if existing_story:
                return await self._store.get_story_graph(conn, story_id=story_id, account_id=account_id)

            tx = conn.transaction()
            await tx.start()
            try:
                project_row = await conn.fetchrow(
                    """
                    select project_id,account_id,owner_user_id,title,description,lifecycle_state,
                           metadata_json,created_at,updated_at
                    from public.v3_projects where project_id=$1 and account_id=$2
                    """,
                    project_id,
                    account_id,
                )
                if project_row:
                    project = Project(
                        project_id=project_row["project_id"],
                        account_id=project_row["account_id"],
                        owner_user_id=project_row["owner_user_id"],
                        title=project_row["title"],
                        description=project_row["description"],
                        lifecycle_state=EntityState(str(project_row["lifecycle_state"])),
                        metadata=dict(project_row["metadata_json"] or {}),
                        created_at=project_row["created_at"],
                        updated_at=project_row["updated_at"],
                    )
                else:
                    if brief.project_id:
                        raise RuntimeError(f"director_project_not_found:{brief.project_id}")
                    project = Project(
                        project_id=project_id,
                        account_id=account_id,
                        owner_user_id=owner_user_id,
                        title=plan.title,
                        description=plan.summary,
                        metadata={"created_by": "creative_director", "thread_id": thread_id},
                        created_at=now,
                        updated_at=now,
                    )
                    await self._store.create_project(conn, project=project)

                existing_participants = await conn.fetch(
                    """
                    select participant_id,display_name
                    from public.v3_participants
                    where project_id=$1 and account_id=$2 and lifecycle_state='active'
                    """,
                    project_id,
                    account_id,
                )
                by_name = {
                    str(row["display_name"]): UUID(str(row["participant_id"]))
                    for row in existing_participants
                    if row["display_name"]
                }

                participants: list[Participant] = []
                for index, item in enumerate(plan.participants):
                    participant_id = by_name.get(item.display_name)
                    row = None

                    if item.participant_id is not None:
                        # Model output may echo an ID from retrieved context, but it
                        # cannot introduce an arbitrary durable identity.
                        row = await conn.fetchrow(
                            """
                            select * from public.v3_participants
                            where participant_id=$1 and account_id=$2 and project_id=$3
                              and lifecycle_state='active'
                            """,
                            item.participant_id,
                            account_id,
                            project_id,
                        )
                        if row:
                            participant_id = UUID(str(row["participant_id"]))

                    participant_id = participant_id or _id(
                        thread_id, "participant", f"{index}:{item.display_name}"
                    )
                    if row is None:
                        row = await conn.fetchrow(
                            """
                            select * from public.v3_participants
                            where participant_id=$1 and account_id=$2 and project_id=$3
                            """,
                            participant_id,
                            account_id,
                            project_id,
                        )

                    if row:
                        participant = Participant(
                            participant_id=row["participant_id"],
                            account_id=row["account_id"],
                            project_id=row["project_id"],
                            kind=row["participant_kind"],
                            display_name=row["display_name"],
                            description=row["description"],
                            default_locale=row["default_locale"],
                            primary_face_media_id=row["primary_face_media_id"],
                            voice_profile_ref=row["voice_profile_ref"],
                            voice_locale=row["voice_locale"],
                            persona=dict(row["persona_json"] or {}),
                            continuity=dict(row["continuity_json"] or {}),
                            lifecycle_state=row["lifecycle_state"],
                            metadata=dict(row["metadata_json"] or {}),
                            created_at=row["created_at"],
                            updated_at=row["updated_at"],
                        )
                    else:
                        participant = Participant(
                            participant_id=participant_id,
                            account_id=account_id,
                            project_id=project_id,
                            kind=item.kind,
                            display_name=item.display_name,
                            description=item.role,
                            default_locale=item.preferred_locale,
                            persona=item.persona,
                            continuity=item.continuity,
                            metadata={
                                "created_by": "creative_director",
                                "visual_direction": item.visual_direction,
                                "voice_direction": item.voice_direction,
                            },
                            created_at=now,
                            updated_at=now,
                        )
                        await self._store.create_participant(conn, participant=participant)
                    participants.append(participant)

                participant_ids = {p.display_name: p.participant_id for p in participants if p.display_name}
                story = Story(
                    story_id=story_id,
                    account_id=account_id,
                    project_id=project_id,
                    title=plan.title,
                    synopsis=plan.summary or plan.logline,
                    default_locale=brief.locale,
                    metadata={
                        "created_by": "creative_director",
                        "thread_id": thread_id,
                        "continuity_plan": plan.continuity_plan,
                        "creative_direction": plan.creative_direction,
                        "retrieved_context_refs": list(plan.retrieved_context_refs),
                        "assumptions": list(plan.assumptions),
                    },
                    created_at=now,
                    updated_at=now,
                )
                await self._store.create_story(conn, story=story)

                story_memberships = []
                for sequence, participant in enumerate(participants):
                    planned = next(p for p in plan.participants if p.display_name == participant.display_name)
                    membership = StoryParticipant(
                        story_id=story_id,
                        participant_id=participant.participant_id,
                        sequence=sequence,
                        role_label=planned.role,
                    )
                    await self._store.add_story_participant(conn, membership=membership)
                    story_memberships.append(membership)

                scenes: list[Scene] = []
                scene_memberships: list[SceneParticipant] = []
                turns: list[DialogueTurn] = []
                for planned_scene in sorted(plan.scenes, key=lambda x: x.sequence):
                    scene_id = _id(thread_id, "scene", str(planned_scene.sequence))
                    scene = Scene(
                        scene_id=scene_id,
                        story_id=story_id,
                        sequence=planned_scene.sequence,
                        title=planned_scene.title,
                        summary=planned_scene.purpose,
                        setting=planned_scene.setting,
                        direction={
                            "visual": planned_scene.visual_direction,
                            "audio": planned_scene.audio_direction,
                            "camera": planned_scene.camera_direction,
                            "performance": planned_scene.performance_direction,
                        },
                        created_at=now,
                        updated_at=now,
                    )
                    await self._store.create_scene(conn, scene=scene)
                    scenes.append(scene)

                    for sequence, name in enumerate(planned_scene.participant_refs):
                        membership = SceneParticipant(
                            scene_id=scene_id,
                            participant_id=participant_ids[name],
                            sequence=sequence,
                            placement=dict(planned_scene.visual_direction.get("placement", {})),
                            performance=planned_scene.performance_direction,
                        )
                        await self._store.add_scene_participant(conn, membership=membership)
                        scene_memberships.append(membership)

                    for item in sorted(planned_scene.dialogue, key=lambda x: x.sequence):
                        speaker_id = participant_ids.get(item.speaker_ref) if item.speaker_ref else None
                        turn = DialogueTurn(
                            turn_id=_id(thread_id, "turn", f"{planned_scene.sequence}:{item.sequence}"),
                            scene_id=scene_id,
                            sequence=item.sequence,
                            kind=item.kind,
                            speaker_participant_id=speaker_id,
                            text=item.text,
                            locale=item.locale or brief.locale,
                            emotion_code=item.emotion,
                            delivery=item.delivery,
                            created_at=now,
                        )
                        await self._store.add_dialogue_turn(conn, turn=turn)
                        turns.append(turn)

                await tx.commit()
                return StoryGraph(
                    project=project,
                    participants=tuple(participants),
                    story=story,
                    story_participants=tuple(story_memberships),
                    scenes=tuple(scenes),
                    scene_participants=tuple(scene_memberships),
                    dialogue_turns=tuple(turns),
                )
            except Exception:
                await tx.rollback()
                raise
