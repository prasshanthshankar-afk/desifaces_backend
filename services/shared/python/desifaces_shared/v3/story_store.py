from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from df_contracts.v3.domain import EntityState, ParticipantKind
from df_contracts.v3.story import (
    DialogueTurn,
    DialogueTurnKind,
    Participant,
    Project,
    Scene,
    SceneParticipant,
    SceneState,
    Story,
    StoryGraph,
    StoryParticipant,
    StoryState,
)


class StoryOwnershipError(RuntimeError):
    pass


class StoryGraphNotFound(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        pass
    try:
        value = row.get(key)
        return default if value is None else value
    except Exception:
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


class CanonicalStoryStore:
    """Provider-neutral persistence boundary for Project -> Participant -> Story.

    This store never calls Face, Audio, Fusion, pricing, storage or an AI provider.
    Orchestration layers compose those capabilities around durable participant,
    scene and dialogue identity.
    """

    async def create_graph(self, conn, *, graph: StoryGraph) -> StoryGraph:
        """Persist a complete graph using caller-owned transaction semantics."""
        await self.create_project(conn, project=graph.project)
        for participant in graph.participants:
            await self.create_participant(conn, participant=participant)
        await self.create_story(conn, story=graph.story)

        memberships = graph.story_participants or tuple(
            StoryParticipant(
                story_id=graph.story.story_id,
                participant_id=participant.participant_id,
                sequence=index,
            )
            for index, participant in enumerate(graph.participants)
        )
        for membership in memberships:
            await self.add_story_participant(conn, membership=membership)

        for scene in sorted(graph.scenes, key=lambda x: x.sequence):
            await self.create_scene(conn, scene=scene)
        for membership in graph.scene_participants:
            await self.add_scene_participant(conn, membership=membership)
        for turn in sorted(graph.dialogue_turns, key=lambda x: (str(x.scene_id), x.sequence)):
            await self.add_dialogue_turn(conn, turn=turn)

        return await self.get_story_graph(
            conn,
            story_id=graph.story.story_id,
            account_id=graph.project.account_id,
        )

    async def create_project(self, conn, *, project: Project) -> Project:
        await conn.execute(
            """
            insert into public.v3_projects(
              project_id,account_id,owner_user_id,title,description,lifecycle_state,
              metadata_json,created_at,updated_at
            ) values($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)
            """,
            project.project_id,
            project.account_id,
            project.owner_user_id,
            project.title,
            project.description,
            project.lifecycle_state.value,
            _json(project.metadata),
            project.created_at,
            project.updated_at,
        )
        return project

    async def create_participant(self, conn, *, participant: Participant) -> Participant:
        await conn.execute(
            """
            insert into public.v3_participants(
              participant_id,account_id,project_id,participant_kind,display_name,description,
              default_locale,primary_face_media_id,voice_profile_ref,voice_locale,
              persona_json,continuity_json,lifecycle_state,metadata_json,created_at,updated_at
            ) values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb,$13,$14::jsonb,$15,$16)
            """,
            participant.participant_id,
            participant.account_id,
            participant.project_id,
            participant.kind.value,
            participant.display_name,
            participant.description,
            participant.default_locale,
            participant.primary_face_media_id,
            participant.voice_profile_ref,
            participant.voice_locale,
            _json(participant.persona),
            _json(participant.continuity),
            participant.lifecycle_state.value,
            _json(participant.metadata),
            participant.created_at,
            participant.updated_at,
        )

        references = list(dict.fromkeys(participant.reference_media_ids))
        for sequence, media_id in enumerate(references):
            relation = "primary_face" if media_id == participant.primary_face_media_id else "reference_face"
            await self.attach_participant_media(
                conn,
                participant_id=participant.participant_id,
                media_id=media_id,
                relation=relation,
                sequence=sequence,
            )
        if participant.primary_face_media_id and participant.primary_face_media_id not in references:
            await self.attach_participant_media(
                conn,
                participant_id=participant.participant_id,
                media_id=participant.primary_face_media_id,
                relation="primary_face",
                sequence=0,
            )
        return participant

    async def attach_participant_media(
        self,
        conn,
        *,
        participant_id: UUID,
        media_id: UUID,
        relation: str = "reference_face",
        sequence: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            """
            insert into public.v3_participant_media(
              participant_id,media_id,relation,sequence_no,metadata_json
            ) values($1,$2,$3,$4,$5::jsonb)
            on conflict(participant_id,media_id,relation)
            do update set sequence_no=excluded.sequence_no,metadata_json=excluded.metadata_json
            """,
            participant_id,
            media_id,
            relation,
            sequence,
            _json(dict(metadata or {})),
        )

    async def bind_participant_identity(
        self,
        conn,
        *,
        participant_id: UUID,
        account_id: UUID,
        primary_face_media_id: UUID | None = None,
        voice_profile_ref: str | None = None,
        voice_locale: str | None = None,
        continuity_patch: Mapping[str, Any] | None = None,
    ) -> None:
        participant_account = await conn.fetchval(
            "select account_id from public.v3_participants where participant_id=$1",
            participant_id,
        )
        if participant_account is None:
            raise StoryGraphNotFound(f"participant_not_found:{participant_id}")
        if UUID(str(participant_account)) != account_id:
            raise StoryOwnershipError(f"participant_account_mismatch:{participant_id}")

        await conn.execute(
            """
            update public.v3_participants
            set primary_face_media_id=coalesce($3,primary_face_media_id),
                voice_profile_ref=coalesce($4,voice_profile_ref),
                voice_locale=coalesce($5,voice_locale),
                continuity_json=coalesce(continuity_json,'{}'::jsonb) || $6::jsonb,
                updated_at=now()
            where participant_id=$1 and account_id=$2
            """,
            participant_id,
            account_id,
            primary_face_media_id,
            voice_profile_ref,
            voice_locale,
            _json(dict(continuity_patch or {})),
        )
        if primary_face_media_id is not None:
            await self.attach_participant_media(
                conn,
                participant_id=participant_id,
                media_id=primary_face_media_id,
                relation="primary_face",
                sequence=0,
            )

    async def create_story(self, conn, *, story: Story) -> Story:
        await conn.execute(
            """
            insert into public.v3_stories(
              story_id,account_id,project_id,title,synopsis,default_locale,state,
              metadata_json,created_at,updated_at
            ) values($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)
            """,
            story.story_id,
            story.account_id,
            story.project_id,
            story.title,
            story.synopsis,
            story.default_locale,
            story.state.value,
            _json(story.metadata),
            story.created_at,
            story.updated_at,
        )
        return story

    async def add_story_participant(self, conn, *, membership: StoryParticipant) -> None:
        await conn.execute(
            """
            insert into public.v3_story_participants(
              story_id,participant_id,sequence_no,role_label,metadata_json
            ) values($1,$2,$3,$4,$5::jsonb)
            on conflict(story_id,participant_id)
            do update set sequence_no=excluded.sequence_no,role_label=excluded.role_label,
                          metadata_json=excluded.metadata_json
            """,
            membership.story_id,
            membership.participant_id,
            membership.sequence,
            membership.role_label,
            _json(membership.metadata),
        )

    async def create_scene(self, conn, *, scene: Scene) -> Scene:
        await conn.execute(
            """
            insert into public.v3_scenes(
              scene_id,story_id,sequence_no,title,summary,setting_json,direction_json,
              duration_hint_ms,state,metadata_json,created_at,updated_at
            ) values($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10::jsonb,$11,$12)
            """,
            scene.scene_id,
            scene.story_id,
            scene.sequence,
            scene.title,
            scene.summary,
            _json(scene.setting),
            _json(scene.direction),
            scene.duration_hint_ms,
            scene.state.value,
            _json(scene.metadata),
            scene.created_at,
            scene.updated_at,
        )
        return scene

    async def add_scene_participant(self, conn, *, membership: SceneParticipant) -> None:
        await conn.execute(
            """
            insert into public.v3_scene_participants(
              scene_id,participant_id,sequence_no,role_label,placement_json,performance_json,metadata_json
            ) values($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb)
            on conflict(scene_id,participant_id)
            do update set sequence_no=excluded.sequence_no,role_label=excluded.role_label,
                          placement_json=excluded.placement_json,performance_json=excluded.performance_json,
                          metadata_json=excluded.metadata_json
            """,
            membership.scene_id,
            membership.participant_id,
            membership.sequence,
            membership.role_label,
            _json(membership.placement),
            _json(membership.performance),
            _json(membership.metadata),
        )

    async def add_dialogue_turn(self, conn, *, turn: DialogueTurn) -> DialogueTurn:
        await conn.execute(
            """
            insert into public.v3_dialogue_turns(
              turn_id,scene_id,sequence_no,turn_kind,speaker_participant_id,text_value,
              locale,emotion_code,delivery_json,start_offset_ms,duration_hint_ms,metadata_json,created_at
            ) values($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12::jsonb,$13)
            """,
            turn.turn_id,
            turn.scene_id,
            turn.sequence,
            turn.kind.value,
            turn.speaker_participant_id,
            turn.text,
            turn.locale,
            turn.emotion_code,
            _json(turn.delivery),
            turn.start_offset_ms,
            turn.duration_hint_ms,
            _json(turn.metadata),
            turn.created_at,
        )
        return turn

    async def get_story_graph(self, conn, *, story_id: UUID, account_id: UUID) -> StoryGraph:
        story_row = await conn.fetchrow(
            """
            select s.*,p.owner_user_id,p.title as project_title,p.description as project_description,
                   p.lifecycle_state as project_lifecycle,p.metadata_json as project_metadata,
                   p.created_at as project_created_at,p.updated_at as project_updated_at
            from public.v3_stories s
            join public.v3_projects p on p.project_id=s.project_id
            where s.story_id=$1 and s.account_id=$2
            """,
            story_id,
            account_id,
        )
        if not story_row:
            raise StoryGraphNotFound(f"story_not_found:{story_id}")

        project = Project(
            project_id=UUID(str(_row_get(story_row, "project_id"))),
            account_id=account_id,
            owner_user_id=UUID(str(_row_get(story_row, "owner_user_id"))),
            title=str(_row_get(story_row, "project_title")),
            description=_row_get(story_row, "project_description"),
            lifecycle_state=EntityState(str(_row_get(story_row, "project_lifecycle"))),
            metadata=_as_dict(_row_get(story_row, "project_metadata")),
            created_at=_row_get(story_row, "project_created_at"),
            updated_at=_row_get(story_row, "project_updated_at"),
        )
        story = Story(
            story_id=UUID(str(_row_get(story_row, "story_id"))),
            account_id=account_id,
            project_id=project.project_id,
            title=str(_row_get(story_row, "title")),
            synopsis=_row_get(story_row, "synopsis"),
            default_locale=_row_get(story_row, "default_locale"),
            state=StoryState(str(_row_get(story_row, "state"))),
            metadata=_as_dict(_row_get(story_row, "metadata_json")),
            created_at=_row_get(story_row, "created_at"),
            updated_at=_row_get(story_row, "updated_at"),
        )

        participant_rows = await conn.fetch(
            """
            select p.*,
              coalesce(array_agg(pm.media_id order by pm.sequence_no,pm.created_at)
                filter(where pm.relation in ('primary_face','reference_face')), '{}'::uuid[]) as reference_media_ids
            from public.v3_story_participants sp
            join public.v3_participants p on p.participant_id=sp.participant_id
            left join public.v3_participant_media pm on pm.participant_id=p.participant_id
            where sp.story_id=$1
            group by p.participant_id
            order by min(sp.sequence_no),p.created_at,p.participant_id
            """,
            story_id,
        )
        participants = tuple(
            Participant(
                participant_id=UUID(str(_row_get(row, "participant_id"))),
                account_id=UUID(str(_row_get(row, "account_id"))),
                project_id=UUID(str(_row_get(row, "project_id"))),
                kind=ParticipantKind(str(_row_get(row, "participant_kind"))),
                display_name=_row_get(row, "display_name"),
                description=_row_get(row, "description"),
                default_locale=_row_get(row, "default_locale"),
                primary_face_media_id=(UUID(str(_row_get(row, "primary_face_media_id"))) if _row_get(row, "primary_face_media_id") else None),
                reference_media_ids=tuple(UUID(str(x)) for x in (_row_get(row, "reference_media_ids", []) or [])),
                voice_profile_ref=_row_get(row, "voice_profile_ref"),
                voice_locale=_row_get(row, "voice_locale"),
                persona=_as_dict(_row_get(row, "persona_json")),
                continuity=_as_dict(_row_get(row, "continuity_json")),
                lifecycle_state=EntityState(str(_row_get(row, "lifecycle_state"))),
                metadata=_as_dict(_row_get(row, "metadata_json")),
                created_at=_row_get(row, "created_at"),
                updated_at=_row_get(row, "updated_at"),
            )
            for row in participant_rows
        )

        story_participant_rows = await conn.fetch(
            "select * from public.v3_story_participants where story_id=$1 order by sequence_no,participant_id",
            story_id,
        )
        story_participants = tuple(
            StoryParticipant(
                story_id=UUID(str(_row_get(row, "story_id"))),
                participant_id=UUID(str(_row_get(row, "participant_id"))),
                sequence=int(_row_get(row, "sequence_no", 0)),
                role_label=_row_get(row, "role_label"),
                metadata=_as_dict(_row_get(row, "metadata_json")),
            )
            for row in story_participant_rows
        )

        scene_rows = await conn.fetch(
            "select * from public.v3_scenes where story_id=$1 order by sequence_no,scene_id",
            story_id,
        )
        scenes = tuple(
            Scene(
                scene_id=UUID(str(_row_get(row, "scene_id"))),
                story_id=story_id,
                sequence=int(_row_get(row, "sequence_no")),
                title=_row_get(row, "title"),
                summary=_row_get(row, "summary"),
                setting=_as_dict(_row_get(row, "setting_json")),
                direction=_as_dict(_row_get(row, "direction_json")),
                duration_hint_ms=_row_get(row, "duration_hint_ms"),
                state=SceneState(str(_row_get(row, "state"))),
                metadata=_as_dict(_row_get(row, "metadata_json")),
                created_at=_row_get(row, "created_at"),
                updated_at=_row_get(row, "updated_at"),
            )
            for row in scene_rows
        )
        scene_ids = [scene.scene_id for scene in scenes]

        scene_participant_rows: Sequence[Any] = []
        dialogue_rows: Sequence[Any] = []
        if scene_ids:
            scene_participant_rows = await conn.fetch(
                """
                select * from public.v3_scene_participants
                where scene_id=any($1::uuid[])
                order by scene_id,sequence_no,participant_id
                """,
                scene_ids,
            )
            dialogue_rows = await conn.fetch(
                """
                select * from public.v3_dialogue_turns
                where scene_id=any($1::uuid[])
                order by scene_id,sequence_no,turn_id
                """,
                scene_ids,
            )

        scene_participants = tuple(
            SceneParticipant(
                scene_id=UUID(str(_row_get(row, "scene_id"))),
                participant_id=UUID(str(_row_get(row, "participant_id"))),
                sequence=int(_row_get(row, "sequence_no", 0)),
                role_label=_row_get(row, "role_label"),
                placement=_as_dict(_row_get(row, "placement_json")),
                performance=_as_dict(_row_get(row, "performance_json")),
                metadata=_as_dict(_row_get(row, "metadata_json")),
            )
            for row in scene_participant_rows
        )
        dialogue_turns = tuple(
            DialogueTurn(
                turn_id=UUID(str(_row_get(row, "turn_id"))),
                scene_id=UUID(str(_row_get(row, "scene_id"))),
                sequence=int(_row_get(row, "sequence_no")),
                kind=DialogueTurnKind(str(_row_get(row, "turn_kind"))),
                speaker_participant_id=(UUID(str(_row_get(row, "speaker_participant_id"))) if _row_get(row, "speaker_participant_id") else None),
                text=str(_row_get(row, "text_value")),
                locale=_row_get(row, "locale"),
                emotion_code=_row_get(row, "emotion_code"),
                delivery=_as_dict(_row_get(row, "delivery_json")),
                start_offset_ms=_row_get(row, "start_offset_ms"),
                duration_hint_ms=_row_get(row, "duration_hint_ms"),
                metadata=_as_dict(_row_get(row, "metadata_json")),
                created_at=_row_get(row, "created_at"),
            )
            for row in dialogue_rows
        )

        return StoryGraph(
            project=project,
            participants=participants,
            story=story,
            story_participants=story_participants,
            scenes=scenes,
            scene_participants=scene_participants,
            dialogue_turns=dialogue_turns,
        )

    async def touch_project(self, conn, *, project_id: UUID, account_id: UUID) -> None:
        status = await conn.execute(
            "update public.v3_projects set updated_at=$3 where project_id=$1 and account_id=$2",
            project_id,
            account_id,
            datetime.now(timezone.utc),
        )
        if str(status).endswith("0"):
            raise StoryGraphNotFound(f"project_not_found:{project_id}")
