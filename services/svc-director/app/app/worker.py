from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from df_contracts.v3.director import DirectorRunState

from .db import close_pools, open_business_pool, open_checkpoint_pool
from .run_store import DirectorRunStore
from .runtime import create_director_graph

logger = logging.getLogger("svc-director-worker")


def _has_interrupt(result: dict) -> bool:
    return bool(result.get("__interrupt__")) if isinstance(result, dict) else False


async def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    business_pool = await open_business_pool()
    checkpoint_pool = await open_checkpoint_pool()
    checkpointer = AsyncPostgresSaver(checkpoint_pool)
    await checkpointer.setup()
    graph = create_director_graph(business_pool, checkpointer)
    if graph is None:
        raise RuntimeError("creative_director_llm_not_configured")

    store = DirectorRunStore()
    try:
        while True:
            async with business_pool.acquire() as conn:
                await store.recover_expired(conn)
                async with conn.transaction():
                    run = await store.claim_next(conn)
            if not run:
                await asyncio.sleep(1.0)
                continue

            run_id = UUID(str(run["run_id"]))
            thread_id = str(run["thread_id"])
            config = {"configurable": {"thread_id": thread_id}}
            try:
                resume_json = dict(run["resume_json"] or {}) if run["resume_json"] else None
                if resume_json is not None:
                    result = await graph.ainvoke(Command(resume=resume_json), config)
                else:
                    result = await graph.ainvoke(
                        {
                            "run_id": str(run_id),
                            "thread_id": thread_id,
                            "account_id": str(run["account_id"]),
                            "owner_user_id": str(run["owner_user_id"]),
                            "phase": DirectorRunState.DRAFTING.value,
                            "brief": dict(run["brief_json"] or {}),
                            "revision_count": 0,
                            "errors": [],
                        },
                        config,
                    )

                async with business_pool.acquire() as conn:
                    if _has_interrupt(result):
                        await store.mark_awaiting_review(conn, run_id=run_id)
                    elif str(result.get("phase") or "") == DirectorRunState.READY.value:
                        workspace = dict(result.get("workspace") or {})
                        await store.mark_ready(
                            conn,
                            run_id=run_id,
                            project_id=UUID(str(workspace["project_id"])) if workspace.get("project_id") else None,
                            story_id=UUID(str(workspace["story_id"])) if workspace.get("story_id") else None,
                        )
                    else:
                        await store.mark_failed(
                            conn,
                            run_id=run_id,
                            error=f"director_run_ended_without_interrupt_or_ready:{result.get('phase')}",
                        )
            except Exception as exc:
                logger.exception("director_run_failed run_id=%s thread_id=%s", run_id, thread_id)
                async with business_pool.acquire() as conn:
                    await store.mark_failed(conn, run_id=run_id, error=f"{type(exc).__name__}:{exc}")
    finally:
        await close_pools()


if __name__ == "__main__":
    asyncio.run(run_forever())
