from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg

from df_contracts.v3.common import ActorType, RequestActor, RequestContext
from df_contracts.v3.domain import (
    GenerationKind,
    GenerationRequest,
    JobState,
    MediaKind,
    MediaRole,
    ProviderExecutionState,
    SafetyState,
)
from desifaces_shared.v3.generation_store import CanonicalGenerationStore, InvalidJobTransition
from desifaces_shared.v3.media_store import CanonicalMediaStore


def _now():
    return datetime.now(timezone.utc)


async def _scalar(conn, sql: str, *args):
    value = await conn.fetchval(sql, *args)
    return int(value or 0)


async def main() -> None:
    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        raise SystemExit("C45_CERT_FAIL=DATABASE_URL_missing")

    conn = await asyncpg.connect(database_url)
    try:
        required = [
            "public.v3_media_assets",
            "public.v3_media_asset_lineage",
            "public.v3_generation_requests",
            "public.v3_generation_jobs",
            "public.v3_provider_executions",
            "public.v3_generation_job_media",
            "public.v3_generation_job_events",
        ]
        for name in required:
            exists = await conn.fetchval("select to_regclass($1)", name)
            if not exists:
                raise RuntimeError(f"required_relation_missing:{name}")
        print("C4_C5_SCHEMA=PASS")

        unresolved_media = await _scalar(
            conn,
            "select count(*) from public.media_assets where account_id is null",
        )
        invalid_role = await _scalar(
            conn,
            "select count(*) from public.media_assets where role is null or role not in ('source','intermediate','preview','final','thumbnail')",
        )
        orphan_lineage = await _scalar(
            conn,
            """
            select count(*)
            from public.v3_media_asset_lineage l
            left join public.media_assets s on s.id=l.source_media_id
            left join public.media_assets d on d.id=l.derived_media_id
            where s.id is null or d.id is null
            """,
        )
        if unresolved_media or invalid_role or orphan_lineage:
            raise RuntimeError(
                f"media_invariant_failure:unresolved_account={unresolved_media},invalid_role={invalid_role},orphan_lineage={orphan_lineage}"
            )
        print("C4_MEDIA_INVARIANTS=PASS")

        actor_row = await conn.fetchrow(
            """
            select bam.user_id, bam.billing_account_id
            from public.pricing_billing_account_members bam
            join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
            where bam.status='active' and ba.status='active'
            order by bam.is_default desc, bam.created_at asc
            limit 1
            """
        )
        if not actor_row:
            raise RuntimeError("no_v3_account_context_for_certification")
        user_id = UUID(str(actor_row["user_id"]))
        account_id = UUID(str(actor_row["billing_account_id"]))

        baseline = {
            "media": await _scalar(conn, "select count(*) from public.media_assets"),
            "lineage": await _scalar(conn, "select count(*) from public.v3_media_asset_lineage"),
            "requests": await _scalar(conn, "select count(*) from public.v3_generation_requests"),
            "jobs": await _scalar(conn, "select count(*) from public.v3_generation_jobs"),
            "exec": await _scalar(conn, "select count(*) from public.v3_provider_executions"),
            "job_media": await _scalar(conn, "select count(*) from public.v3_generation_job_media"),
            "events": await _scalar(conn, "select count(*) from public.v3_generation_job_events"),
        }

        tx = conn.transaction()
        await tx.start()
        try:
            media_store = CanonicalMediaStore()
            generation_store = CanonicalGenerationStore()

            source = await media_store.create(
                conn,
                account_id=account_id,
                owner_user_id=user_id,
                kind=MediaKind.IMAGE,
                role=MediaRole.SOURCE,
                storage_uri=f"az://v3-cert/source/{uuid4()}.png",
                mime_type="image/png",
                metadata={"certification": "V3-C4"},
            )
            final = await media_store.create(
                conn,
                account_id=account_id,
                owner_user_id=user_id,
                kind=MediaKind.VIDEO,
                role=MediaRole.FINAL,
                storage_uri=f"az://v3-cert/final/{uuid4()}.mp4",
                mime_type="video/mp4",
                source_media_ids=(source.media_id,),
                metadata={"certification": "V3-C4"},
            )
            reread = await media_store.get(conn, media_id=final.media_id, account_id=account_id)
            if reread.source_media_ids != (source.media_id,) or reread.role != MediaRole.FINAL:
                raise RuntimeError("canonical_media_roundtrip_failed")
            print("C4_MEDIA_WRITE_READ_LINEAGE=PASS")

            ctx = RequestContext(
                actor=RequestActor(
                    actor_type=ActorType.USER,
                    actor_id=user_id,
                    account_id=account_id,
                ),
                idempotency_key="v3-c45-certification-idempotency",
                client_app="v3-certifier",
            )
            req = GenerationRequest(
                account_id=account_id,
                requested_by_user_id=user_id,
                kind=GenerationKind.FUSION,
                source_media_ids=(source.media_id,),
                parameters={"certification": True},
                safety_state=SafetyState.ALLOWED,
                created_at=_now(),
            )
            persisted = await generation_store.create_request_and_root_job(
                conn,
                request=req,
                context=ctx,
                idempotency_key="v3-c45-certification-idempotency",
                request_digest="v3-c45-certification-digest",
                initial_state=JobState.SUBMITTED,
                metadata={"certification": "V3-C5"},
            )
            replay = await generation_store.create_request_and_root_job(
                conn,
                request=req,
                context=ctx,
                idempotency_key="v3-c45-certification-idempotency",
                request_digest="v3-c45-certification-digest",
            )
            if replay.created or replay.job_id != persisted.job_id or replay.generation_id != persisted.generation_id:
                raise RuntimeError("generation_idempotent_replay_failed")
            print("C5_GENERATION_IDEMPOTENCY=PASS")

            execution = await generation_store.register_provider_execution(
                conn,
                job_id=persisted.job_id,
                provider="cert-provider",
                capability="certification",
                model="none",
                idempotency_key="v3-c45-provider-attempt-1",
            )
            await generation_store.update_provider_execution(
                conn,
                execution_id=execution.execution_id,
                state=ProviderExecutionState.RUNNING,
                provider_request_id="cert-no-external-call",
            )
            await generation_store.attach_media(conn, job_id=persisted.job_id, media=source, relation="input")
            await generation_store.attach_media(conn, job_id=persisted.job_id, media=final, relation="output")
            await generation_store.transition(conn, job_id=persisted.job_id, target=JobState.QUEUED, context=ctx)
            await generation_store.transition(conn, job_id=persisted.job_id, target=JobState.RUNNING, context=ctx, progress_percent=50)
            await generation_store.update_provider_execution(conn, execution_id=execution.execution_id, state=ProviderExecutionState.SUCCEEDED)
            completed = await generation_store.transition(conn, job_id=persisted.job_id, target=JobState.SUCCEEDED, context=ctx, progress_percent=100)
            if final.media_id not in completed.output_media_ids or completed.state != JobState.SUCCEEDED:
                raise RuntimeError("generation_output_linkage_failed")
            try:
                await generation_store.transition(conn, job_id=persisted.job_id, target=JobState.RUNNING, context=ctx)
            except InvalidJobTransition:
                pass
            else:
                raise RuntimeError("terminal_generation_job_resurrection_allowed")
            print("C5_JOB_PROVIDER_MEDIA_STATE_MACHINE=PASS")
        finally:
            await tx.rollback()

        after = {
            "media": await _scalar(conn, "select count(*) from public.media_assets"),
            "lineage": await _scalar(conn, "select count(*) from public.v3_media_asset_lineage"),
            "requests": await _scalar(conn, "select count(*) from public.v3_generation_requests"),
            "jobs": await _scalar(conn, "select count(*) from public.v3_generation_jobs"),
            "exec": await _scalar(conn, "select count(*) from public.v3_provider_executions"),
            "job_media": await _scalar(conn, "select count(*) from public.v3_generation_job_media"),
            "events": await _scalar(conn, "select count(*) from public.v3_generation_job_events"),
        }
        if after != baseline:
            raise RuntimeError(f"c45_certification_rollback_failed:before={baseline}:after={after}")
        print("C4_C5_CERTIFICATION_ROLLBACK=PASS")
        print("V3_C4_C5_RUNTIME_CERTIFICATION=PASS")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
