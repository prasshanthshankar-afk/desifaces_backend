from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from desifaces_shared.v3.studio_workflow_store import (
    CanonicalStudioWorkflowStore,
)


class StoryFinalBridgeError(RuntimeError):
    pass


def _clean(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class StoryFinalContext:
    workflow_id: UUID
    stage_run_id: UUID
    account_id: UUID
    owner_user_id: UUID
    project_id: UUID
    story_id: UUID
    stage_state: str


async def load_story_final_context(
    conn,
    *,
    account_id: UUID,
    workflow_id: UUID,
    stage_run_id: UUID,
) -> StoryFinalContext:

    row = await conn.fetchrow(
        """
        select
          s.stage_run_id,
          s.workflow_id,
          s.stage_type,
          s.scope_type,
          s.state,
          w.account_id,
          w.owner_user_id,
          w.project_id,
          w.story_id,
          w.current_stage,
          w.state as workflow_state
        from public.v3_studio_stage_runs s
        join public.v3_studio_workflows w
          on w.workflow_id=s.workflow_id
        where s.stage_run_id=$1
          and s.workflow_id=$2
          and w.account_id=$3
        """,
        stage_run_id,
        workflow_id,
        account_id,
    )

    if not row:
        raise StoryFinalBridgeError(
            "story_final_stage_not_found_or_account_mismatch"
        )

    if (
        _clean(row["stage_type"])
        != "story_final"
        or _clean(row["scope_type"])
        != "story"
    ):
        raise StoryFinalBridgeError(
            "story_final_stage_type_scope_mismatch"
        )

    if _clean(row["current_stage"]) != "story_final":
        raise StoryFinalBridgeError(
            "story_final_stage_not_current"
        )

    if _clean(row["workflow_state"]) != "active":
        raise StoryFinalBridgeError(
            "story_final_workflow_not_active"
        )

    if not row["story_id"]:
        raise StoryFinalBridgeError(
            "story_final_story_id_required"
        )

    return StoryFinalContext(
        workflow_id=UUID(
            str(row["workflow_id"])
        ),
        stage_run_id=UUID(
            str(row["stage_run_id"])
        ),
        account_id=UUID(
            str(row["account_id"])
        ),
        owner_user_id=UUID(
            str(row["owner_user_id"])
        ),
        project_id=UUID(
            str(row["project_id"])
        ),
        story_id=UUID(
            str(row["story_id"])
        ),
        stage_state=_clean(row["state"]),
    )


class StoryStitchClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(
            timeout_seconds
        )

    async def stitch(
        self,
        *,
        headers: dict[str, str],
        context: StoryFinalContext,
    ) -> dict[str, Any]:

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout_seconds,
        ) as client:
            response = await client.post(
                "/api/longform/v3/story-stitch",
                json={
                    "project_id":
                        str(context.project_id),
                    "workflow_id":
                        str(context.workflow_id),
                    "stage_run_id":
                        str(context.stage_run_id),
                },
            )

        if response.status_code != 200:
            raise StoryFinalBridgeError(
                "story_final_stitch_failed:"
                f"{response.status_code}:"
                f"{response.text[:1600]}"
            )

        payload = response.json()

        if not _clean(payload.get("media_id")):
            raise StoryFinalBridgeError(
                "story_final_stitch_missing_media_id"
            )

        if not _clean(payload.get("video_url")):
            raise StoryFinalBridgeError(
                "story_final_stitch_missing_video_url"
            )

        return payload


class StoryFinalExecutionService:
    def __init__(
        self,
        *,
        fusion_extension_base_url: str,
        store: CanonicalStudioWorkflowStore
        | None = None,
    ) -> None:
        self.client = StoryStitchClient(
            base_url=fusion_extension_base_url
        )
        self.store = (
            store
            or CanonicalStudioWorkflowStore()
        )

    async def stitch(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> dict[str, Any]:

        async with pool.acquire() as conn:
            context = (
                await load_story_final_context(
                    conn,
                    account_id=account_id,
                    workflow_id=workflow_id,
                    stage_run_id=stage_run_id,
                )
            )

            if context.stage_state not in {
                "pending",
                "ready",
                "failed",
                "rejected",
            }:
                raise StoryFinalBridgeError(
                    "story_final_stage_not_stitchable:"
                    f"{context.stage_state}"
                )

            await self.store.assert_startable(
                conn,
                stage_run_id=stage_run_id,
            )

            await self.store.mark_generating(
                conn,
                stage_run_id=stage_run_id,
            )

        try:
            result = await self.client.stitch(
                headers=headers,
                context=context,
            )

            media_id = UUID(
                str(result["media_id"])
            )

            async with pool.acquire() as conn:
                async with conn.transaction():
                    review_item_id = (
                        await self.store.attach_output(
                            conn,
                            stage_run_id=stage_run_id,
                            media_id=media_id,
                            output_role=(
                                "approved_story_video"
                            ),
                        )
                    )

            return {
                "workflow_id":
                    str(context.workflow_id),
                "stage_run_id":
                    str(context.stage_run_id),
                "story_id":
                    str(context.story_id),
                "stage_state":
                    "awaiting_review",
                "media_asset_id":
                    str(media_id),
                "review_item_id":
                    str(review_item_id),
                "video_url":
                    result["video_url"],
                "scene_count":
                    int(
                        result.get(
                            "scene_count"
                        )
                        or 0
                    ),
                "reused":
                    bool(
                        result.get(
                            "reused",
                            False,
                        )
                    ),
                "assembly_key":
                    _clean(
                        result.get(
                            "assembly_key"
                        )
                    ),
            }

        except Exception as exc:
            try:
                async with pool.acquire() as conn:
                    await self.store.mark_failed(
                        conn,
                        stage_run_id=stage_run_id,
                        error=str(exc),
                    )
            except Exception:
                pass

            if isinstance(
                exc,
                StoryFinalBridgeError,
            ):
                raise

            raise StoryFinalBridgeError(
                "story_final_execution_failed:"
                f"{str(exc)[:1600]}"
            ) from exc


__all__ = [
    "StoryFinalBridgeError",
    "StoryFinalExecutionService",
]
