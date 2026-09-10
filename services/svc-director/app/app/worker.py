from __future__ import annotations

import asyncio
import logging
import socket
import time
from uuid import UUID

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg import OperationalError as PsycopgOperationalError

from df_contracts.v3.director import DirectorRunState

from .db import close_pools, open_business_pool, open_checkpoint_pool
from .run_store import DirectorRunStore
from .runtime import create_director_graph

logger = logging.getLogger("svc-director-worker")

_TRANSIENT_TEXT = (
    "temporary failure in name resolution",
    "name or service not known",
    "connection refused",
    "connection reset",
    "connection closed",
    "server closed the connection unexpectedly",
    "terminating connection due to administrator command",
    "cannot connect now",
    "connection is closed",
)


def _has_interrupt(result: dict) -> bool:
    return bool(result.get("__interrupt__")) if isinstance(result, dict) else False


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_transient_infrastructure_error(exc: BaseException) -> bool:
    for current in _exception_chain(exc):
        if isinstance(
            current,
            (
                socket.gaierror,
                ConnectionError,
                TimeoutError,
                asyncio.TimeoutError,
                asyncpg.PostgresConnectionError,
                PsycopgOperationalError,
            ),
        ):
            return True
        text = str(current).casefold()
        if any(token in text for token in _TRANSIENT_TEXT):
            return True
    return False


async def _run_worker_session() -> None:
    business_pool = None
    try:
        business_pool = await open_business_pool()
        checkpoint_pool = await open_checkpoint_pool()
        checkpointer = AsyncPostgresSaver(checkpoint_pool)
        await checkpointer.setup()
        graph = create_director_graph(business_pool, checkpointer)
        if graph is None:
            raise RuntimeError("creative_director_llm_not_configured")

        store = DirectorRunStore()
        while True:
            # Queue recovery + claim are infrastructure operations. Any transient
            # DNS/DB failure escapes to the outer supervisor, which closes stale
            # pools, backs off, and creates a fresh session instead of killing PID 1.
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
            started = time.perf_counter()
            is_resume = bool(run["resume_json"])
            logger.info(
                "director_run_started run_id=%s thread_id=%s mode=%s",
                run_id,
                thread_id,
                "resume" if is_resume else "initial",
            )
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
                        outcome = "awaiting_review"
                    elif str(result.get("phase") or "") == DirectorRunState.READY.value:
                        workspace = dict(result.get("workspace") or {})
                        await store.mark_ready(
                            conn,
                            run_id=run_id,
                            project_id=UUID(str(workspace["project_id"])) if workspace.get("project_id") else None,
                            story_id=UUID(str(workspace["story_id"])) if workspace.get("story_id") else None,
                        )
                        outcome = "ready"
                    else:
                        await store.mark_failed(
                            conn,
                            run_id=run_id,
                            error=f"director_run_ended_without_interrupt_or_ready:{result.get('phase')}",
                        )
                        outcome = "failed"
                logger.info(
                    "director_run_completed run_id=%s thread_id=%s mode=%s outcome=%s duration_ms=%d",
                    run_id,
                    thread_id,
                    "resume" if is_resume else "initial",
                    outcome,
                    int((time.perf_counter() - started) * 1000),
                )
            except Exception as exc:
                if _is_transient_infrastructure_error(exc):
                    logger.warning(
                        "director_run_transient_infrastructure_failure run_id=%s thread_id=%s error=%s",
                        run_id,
                        thread_id,
                        type(exc).__name__,
                        exc_info=True,
                    )
                    # If DB is reachable again, release the lease immediately for a
                    # bounded retry. If this requeue itself cannot reach DB, let the
                    # outer supervisor rebuild pools; lease recovery handles the row.
                    async with business_pool.acquire() as conn:
                        requeued = await store.requeue_transient(
                            conn,
                            run_id=run_id,
                            error=f"{type(exc).__name__}:{exc}",
                            delay_seconds=2,
                        )
                    if requeued:
                        logger.info(
                            "director_run_requeued_after_transient run_id=%s thread_id=%s",
                            run_id,
                            thread_id,
                        )
                        continue

                logger.exception("director_run_failed run_id=%s thread_id=%s", run_id, thread_id)
                async with business_pool.acquire() as conn:
                    await store.mark_failed(conn, run_id=run_id, error=f"{type(exc).__name__}:{exc}")
                logger.info(
                    "director_run_completed run_id=%s thread_id=%s mode=%s outcome=failed duration_ms=%d",
                    run_id,
                    thread_id,
                    "resume" if is_resume else "initial",
                    int((time.perf_counter() - started) * 1000),
                )
    finally:
        await close_pools()


async def run_forever() -> None:
    """Supervise Director queue consumption across transient DB/DNS failures."""
    logging.basicConfig(level=logging.INFO)
    failure_count = 0
    while True:
        try:
            await _run_worker_session()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_transient_infrastructure_error(exc):
                raise
            failure_count += 1
            delay = min(30.0, 0.5 * (2 ** min(failure_count - 1, 6)))
            logger.warning(
                "director_worker_transient_retry attempt=%d delay_seconds=%.1f error=%s",
                failure_count,
                delay,
                type(exc).__name__,
                exc_info=True,
            )
            try:
                await close_pools()
            except Exception:
                logger.warning("director_worker_pool_cleanup_failed", exc_info=True)
            await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(run_forever())
