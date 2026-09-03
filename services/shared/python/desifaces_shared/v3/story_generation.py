from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from df_contracts.v3.common import RequestContext
from df_contracts.v3.domain import GenerationRequest, JobState

from .generation_store import CanonicalGenerationStore, GenerationPersistenceResult
from .story_store import StoryGraphNotFound, StoryOwnershipError


@dataclass(frozen=True)
class StoryGenerationPersistenceResult:
    generation_id: UUID
    job_id: UUID
    created: bool
    story_id: UUID | None
    scene_id: UUID | None


class CanonicalStoryGenerationStore:
    """C5 bridge that validates Story/Scene/Participant context before generation.

    The underlying GenerationStore remains capability-neutral. This wrapper is
    the required persistence entry point for Story-driven Face/Audio/Fusion work.
    It does not call providers or pricing.
    """

    def __init__(self, generation_store: CanonicalGenerationStore | None = None) -> None:
        self._generation_store = generation_store or CanonicalGenerationStore()

    async def create_request_and_root_job(
        self,
        conn,
        *,
        request: GenerationRequest,
        context: RequestContext,
        idempotency_key: str,
        request_digest: str,
        initial_state: JobState = JobState.SUBMITTED,
        compatibility_service: str | None = None,
        compatibility_job_id: str | None = None,
        compatibility: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> StoryGenerationPersistenceResult:
        await self._validate_context(conn, request=request)

        result: GenerationPersistenceResult = await self._generation_store.create_request_and_root_job(
            conn,
            request=request,
            context=context,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            initial_state=initial_state,
            compatibility_service=compatibility_service,
            compatibility_job_id=compatibility_job_id,
            compatibility=compatibility,
            metadata=metadata,
        )

        if request.story_id is not None or request.scene_id is not None:
            await conn.execute(
                """
                update public.v3_generation_requests
                set story_id=$2,scene_id=$3
                where generation_id=$1 and account_id=$4
                """,
                result.generation_id,
                request.story_id,
                request.scene_id,
                request.account_id,
            )

        return StoryGenerationPersistenceResult(
            generation_id=result.generation_id,
            job_id=result.job_id,
            created=result.created,
            story_id=request.story_id,
            scene_id=request.scene_id,
        )

    async def _validate_context(self, conn, *, request: GenerationRequest) -> None:
        if request.scene_id is not None and request.story_id is None:
            raise ValueError("scene_generation_requires_story_id")

        if request.project_id is not None:
            project_account = await conn.fetchval(
                "select account_id from public.v3_projects where project_id=$1",
                request.project_id,
            )
            if project_account is None:
                raise StoryGraphNotFound(f"project_not_found:{request.project_id}")
            if UUID(str(project_account)) != request.account_id:
                raise StoryOwnershipError(f"project_account_mismatch:{request.project_id}")

        if request.story_id is not None:
            story = await conn.fetchrow(
                "select account_id,project_id from public.v3_stories where story_id=$1",
                request.story_id,
            )
            if not story:
                raise StoryGraphNotFound(f"story_not_found:{request.story_id}")
            story_account = UUID(str(story["account_id"]))
            story_project = UUID(str(story["project_id"]))
            if story_account != request.account_id:
                raise StoryOwnershipError(f"story_account_mismatch:{request.story_id}")
            if request.project_id is not None and story_project != request.project_id:
                raise ValueError("generation_story_project_mismatch")

        if request.scene_id is not None:
            scene_story = await conn.fetchval(
                "select story_id from public.v3_scenes where scene_id=$1",
                request.scene_id,
            )
            if scene_story is None:
                raise StoryGraphNotFound(f"scene_not_found:{request.scene_id}")
            if UUID(str(scene_story)) != request.story_id:
                raise ValueError("generation_scene_story_mismatch")

        if request.participant_ids:
            rows = await conn.fetch(
                """
                select participant_id,account_id,project_id
                from public.v3_participants
                where participant_id=any($1::uuid[])
                """,
                list(request.participant_ids),
            )
            found = {UUID(str(row["participant_id"])): row for row in rows}
            if set(request.participant_ids) != set(found):
                missing = sorted(str(x) for x in set(request.participant_ids) - set(found))
                raise StoryGraphNotFound(f"generation_participant_not_found:{','.join(missing)}")
            for participant_id, row in found.items():
                if UUID(str(row["account_id"])) != request.account_id:
                    raise StoryOwnershipError(f"participant_account_mismatch:{participant_id}")
                if request.project_id is not None and UUID(str(row["project_id"])) != request.project_id:
                    raise ValueError(f"generation_participant_project_mismatch:{participant_id}")

            if request.story_id is not None:
                story_members = set(
                    UUID(str(x))
                    for x in await conn.fetchval(
                        """
                        select coalesce(array_agg(participant_id),'{}'::uuid[])
                        from public.v3_story_participants where story_id=$1
                        """,
                        request.story_id,
                    )
                )
                if not set(request.participant_ids).issubset(story_members):
                    raise ValueError("generation_participant_not_in_story")

            if request.scene_id is not None:
                scene_members = set(
                    UUID(str(x))
                    for x in await conn.fetchval(
                        """
                        select coalesce(array_agg(participant_id),'{}'::uuid[])
                        from public.v3_scene_participants where scene_id=$1
                        """,
                        request.scene_id,
                    )
                )
                if not set(request.participant_ids).issubset(scene_members):
                    raise ValueError("generation_participant_not_in_scene")
