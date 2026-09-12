from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from df_contracts.v3.domain import MediaKind, MediaRole
from df_contracts.v3.studio_workflow import ReviewDecision, StudioStageType
from desifaces_shared.v3.media_store import CanonicalMediaStore
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore

from app.db import close_pools, open_business_pool
from app.studio_workflow import build_direct_studio_workflow


async def _actor(conn) -> tuple[UUID, UUID]:
    row = await conn.fetchrow(
        """select bam.user_id,bam.billing_account_id
        from public.pricing_billing_account_members bam
        join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
        join core.users u on u.id=bam.user_id
        where bam.status='active' and ba.status='active'
        order by bam.is_default desc,bam.created_at asc limit 1"""
    )
    if not row:
        raise RuntimeError("STUDIO_HITL_FAIL=no_active_actor")
    return UUID(str(row["user_id"])), UUID(str(row["billing_account_id"]))


async def _expect_db_reject(conn, op, marker: str) -> None:
    rejected = False
    try:
        async with conn.transaction():
            await op()
    except Exception:
        rejected = True
    if not rejected:
        raise RuntimeError(f"STUDIO_HITL_FAIL=expected_reject:{marker}")


async def main() -> None:
    pool = await open_business_pool()
    studio = CanonicalStudioWorkflowStore()
    media = CanonicalMediaStore()
    try:
        async with pool.acquire() as conn:
            before = {
                "projects": int(await conn.fetchval("select count(*) from public.v3_projects")),
                "workflows": int(await conn.fetchval("select count(*) from public.v3_studio_workflows")),
                "media": int(await conn.fetchval("select count(*) from public.media_assets")),
            }
            tx = conn.transaction()
            await tx.start()
            try:
                user_id, account_id = await _actor(conn)
                project_id, participant_id = uuid4(), uuid4()
                await conn.execute(
                    """insert into public.v3_projects(project_id,account_id,owner_user_id,title)
                    values($1,$2,$3,'Direct Studio HITL Functional Test')""",
                    project_id, account_id, user_id,
                )
                await conn.execute(
                    """insert into public.v3_participants(
                      participant_id,account_id,project_id,participant_kind,display_name
                    ) values($1,$2,$3,'person','Functional Participant')""",
                    participant_id, account_id, project_id,
                )

                workflow_id = await build_direct_studio_workflow(
                    conn,
                    account_id=account_id,
                    owner_user_id=user_id,
                    project_id=project_id,
                    participant_id=participant_id,
                    store=studio,
                )
                view = await studio.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
                by_type = {stage.stage_type: stage for stage in view.stages}
                if set(by_type) != {StudioStageType.FACE, StudioStageType.AUDIO, StudioStageType.FUSION}:
                    raise RuntimeError(f"STUDIO_HITL_FAIL=direct_stage_graph:{set(by_type)}")
                face_stage, audio_stage, fusion_stage = (
                    by_type[StudioStageType.FACE], by_type[StudioStageType.AUDIO], by_type[StudioStageType.FUSION]
                )
                print("STUDIO_HITL_DIRECT_GRAPH=PASS")

                await studio.mark_generating(conn, stage_run_id=face_stage.stage_run_id)
                face_asset = await media.create(
                    conn, account_id=account_id, owner_user_id=user_id, project_id=project_id,
                    kind=MediaKind.IMAGE, role=MediaRole.FINAL,
                    storage_uri=f"functional://face/{uuid4()}", metadata={"functional_test": True},
                )
                face_review = await studio.attach_output(
                    conn, stage_run_id=face_stage.stage_run_id, media_id=face_asset.media_id, output_role="face",
                )
                print("STUDIO_HITL_FACE_OUTPUT_REVIEW_PENDING=PASS")

                await _expect_db_reject(
                    conn,
                    lambda: studio.bind_approved_input(
                        conn, stage_run_id=audio_stage.stage_run_id, media_id=face_asset.media_id,
                        input_role="face", source_stage_run_id=face_stage.stage_run_id,
                    ),
                    "audio_consumed_unapproved_face",
                )
                print("STUDIO_HITL_AUDIO_BLOCKED_BEFORE_FACE_APPROVAL=PASS")

                await studio.review_output(
                    conn, review_item_id=face_review, reviewer_user_id=user_id,
                    decision=ReviewDecision.APPROVED, feedback="Face approved",
                )
                await studio.bind_approved_input(
                    conn, stage_run_id=audio_stage.stage_run_id, media_id=face_asset.media_id,
                    input_role="face", source_stage_run_id=face_stage.stage_run_id,
                )
                await studio.mark_generating(conn, stage_run_id=audio_stage.stage_run_id)
                print("STUDIO_HITL_APPROVED_FACE_TO_AUDIO=PASS")

                audio_asset = await media.create(
                    conn, account_id=account_id, owner_user_id=user_id, project_id=project_id,
                    kind=MediaKind.AUDIO, role=MediaRole.FINAL,
                    storage_uri=f"functional://audio/{uuid4()}", metadata={"functional_test": True},
                )
                audio_review = await studio.attach_output(
                    conn, stage_run_id=audio_stage.stage_run_id, media_id=audio_asset.media_id, output_role="audio",
                )
                print("STUDIO_HITL_AUDIO_OUTPUT_REVIEW_PENDING=PASS")

                await studio.bind_approved_input(
                    conn, stage_run_id=fusion_stage.stage_run_id, media_id=face_asset.media_id,
                    input_role="face", source_stage_run_id=face_stage.stage_run_id,
                )
                await _expect_db_reject(
                    conn,
                    lambda: studio.bind_approved_input(
                        conn, stage_run_id=fusion_stage.stage_run_id, media_id=audio_asset.media_id,
                        input_role="audio", source_stage_run_id=audio_stage.stage_run_id,
                    ),
                    "fusion_consumed_unapproved_audio",
                )
                print("STUDIO_HITL_FUSION_BLOCKED_BEFORE_AUDIO_APPROVAL=PASS")

                await studio.review_output(
                    conn, review_item_id=audio_review, reviewer_user_id=user_id,
                    decision=ReviewDecision.APPROVED, feedback="Audio approved",
                )
                await studio.bind_approved_input(
                    conn, stage_run_id=fusion_stage.stage_run_id, media_id=audio_asset.media_id,
                    input_role="audio", source_stage_run_id=audio_stage.stage_run_id,
                )
                await studio.mark_generating(conn, stage_run_id=fusion_stage.stage_run_id)
                print("STUDIO_HITL_APPROVED_FACE_AUDIO_TO_FUSION=PASS")

                video_1 = await media.create(
                    conn, account_id=account_id, owner_user_id=user_id, project_id=project_id,
                    kind=MediaKind.VIDEO, role=MediaRole.FINAL,
                    storage_uri=f"functional://video/{uuid4()}", metadata={"functional_test": True, "variant": 1},
                )
                video_2 = await media.create(
                    conn, account_id=account_id, owner_user_id=user_id, project_id=project_id,
                    kind=MediaKind.VIDEO, role=MediaRole.FINAL,
                    storage_uri=f"functional://video/{uuid4()}", metadata={"functional_test": True, "variant": 2},
                )
                review_1 = await studio.attach_output(
                    conn, stage_run_id=fusion_stage.stage_run_id, media_id=video_1.media_id, output_role="video_variant",
                )
                review_2 = await studio.attach_output(
                    conn, stage_run_id=fusion_stage.stage_run_id, media_id=video_2.media_id, output_role="video_variant",
                )
                await studio.review_output(
                    conn, review_item_id=review_1, reviewer_user_id=user_id,
                    decision=ReviewDecision.APPROVED, feedback="Selected video",
                )
                await studio.review_output(
                    conn, review_item_id=review_2, reviewer_user_id=user_id,
                    decision=ReviewDecision.REVISE, feedback="Supersede this variant",
                )
                final_view = await studio.get_workflow(conn, workflow_id=workflow_id, account_id=account_id)
                final_fusion = next(stage for stage in final_view.stages if stage.stage_type == StudioStageType.FUSION)
                active_outputs = [item for item in final_fusion.outputs if item.is_active]
                inactive_outputs = [item for item in final_fusion.outputs if not item.is_active]
                if final_fusion.state.value != "approved" or len(active_outputs) != 1 or len(inactive_outputs) != 1:
                    raise RuntimeError(f"STUDIO_HITL_FAIL=variant_selection:{final_fusion}")
                print("STUDIO_HITL_FUSION_VARIANT_APPROVAL=PASS")
                print("V3_STUDIO_HITL_DIRECT_FUNCTIONAL=PASS")
            finally:
                await tx.rollback()

            after = {
                "projects": int(await conn.fetchval("select count(*) from public.v3_projects")),
                "workflows": int(await conn.fetchval("select count(*) from public.v3_studio_workflows")),
                "media": int(await conn.fetchval("select count(*) from public.media_assets")),
            }
            if before != after:
                raise RuntimeError(f"STUDIO_HITL_FAIL=rollback_drift:before={before}:after={after}")
            print("STUDIO_HITL_TRANSACTION_ROLLBACK=PASS")
    finally:
        await close_pools()


if __name__ == "__main__":
    asyncio.run(main())
